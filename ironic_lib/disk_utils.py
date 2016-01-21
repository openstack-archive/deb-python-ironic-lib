# Copyright 2014 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import base64
import gzip
import logging
import math
import os
import re
import requests
import shutil
import six
import stat
import tempfile
import time

from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_utils import excutils
from oslo_utils import units

from ironic_lib.openstack.common import imageutils

from ironic_lib.common.i18n import _
from ironic_lib.common.i18n import _LE
from ironic_lib.common.i18n import _LI
from ironic_lib.common.i18n import _LW
from ironic_lib import disk_partitioner
from ironic_lib import exception
from ironic_lib import utils


opts = [
    cfg.IntOpt('efi_system_partition_size',
               default=200,
               help='Size of EFI system partition in MiB when configuring '
                    'UEFI systems for local boot.',
               deprecated_group='deploy'),
    cfg.StrOpt('dd_block_size',
               default='1M',
               help='Block size to use when writing to the nodes disk.',
               deprecated_group='deploy'),
    cfg.IntOpt('iscsi_verify_attempts',
               default=3,
               help='Maximum attempts to verify an iSCSI connection is '
                    'active, sleeping 1 second between attempts.',
               deprecated_group='deploy'),
]

CONF = cfg.CONF
CONF.register_opts(opts, group='disk_utils')

LOG = logging.getLogger(__name__)

_PARTED_PRINT_RE = re.compile(r"^(\d+):([\d\.]+)MiB:"
                              "([\d\.]+)MiB:([\d\.]+)MiB:(\w*)::(\w*)")


def list_partitions(device):
    """Get partitions information from given device.

    :param device: The device path.
    :returns: list of dictionaries (one per partition) with keys:
              number, start, end, size (in MiB), filesystem, flags
    """
    output = utils.execute(
        'parted', '-s', '-m', device, 'unit', 'MiB', 'print',
        use_standard_locale=True, run_as_root=True)[0]
    if isinstance(output, bytes):
        output = output.decode("utf-8")
    lines = [line for line in output.split('\n') if line.strip()][2:]
    # Example of line: 1:1.00MiB:501MiB:500MiB:ext4::boot
    fields = ('number', 'start', 'end', 'size', 'filesystem', 'flags')
    result = []
    for line in lines:
        match = _PARTED_PRINT_RE.match(line)
        if match is None:
            LOG.warning(_LW("Partition information from parted for device "
                            "%(device)s does not match "
                            "expected format: %(line)s"),
                        dict(device=device, line=line))
            continue
        # Cast int fields to ints (some are floats and we round them down)
        groups = [int(float(x)) if i < 4 else x
                  for i, x in enumerate(match.groups())]
        result.append(dict(zip(fields, groups)))
    return result


def get_disk_identifier(dev):
    """Get the disk identifier from the disk being exposed by the ramdisk.

    This disk identifier is appended to the pxe config which will then be
    used by chain.c32 to detect the correct disk to chainload. This is helpful
    in deployments to nodes with multiple disks.

    http://www.syslinux.org/wiki/index.php/Comboot/chain.c32#mbr:

    :param dev: Path for the already populated disk device.
    :returns The Disk Identifier.
    """
    disk_identifier = utils.execute('hexdump', '-s', '440', '-n', '4',
                                    '-e', '''\"0x%08x\"''',
                                    dev,
                                    run_as_root=True,
                                    check_exit_code=[0],
                                    attempts=5,
                                    delay_on_retry=True)
    return disk_identifier[0]


def make_partitions(dev, root_mb, swap_mb, ephemeral_mb,
                    configdrive_mb, node_uuid, commit=True,
                    boot_option="netboot", boot_mode="bios"):
    """Partition the disk device.

    Create partitions for root, swap, ephemeral and configdrive on a
    disk device.

    :param root_mb: Size of the root partition in mebibytes (MiB).
    :param swap_mb: Size of the swap partition in mebibytes (MiB). If 0,
        no partition will be created.
    :param ephemeral_mb: Size of the ephemeral partition in mebibytes (MiB).
        If 0, no partition will be created.
    :param configdrive_mb: Size of the configdrive partition in
        mebibytes (MiB). If 0, no partition will be created.
    :param commit: True/False. Default for this setting is True. If False
        partitions will not be written to disk.
    :param boot_option: Can be "local" or "netboot". "netboot" by default.
    :param boot_mode: Can be "bios" or "uefi". "bios" by default.
    :param node_uuid: Node's uuid. Used for logging.
    :returns: A dictionary containing the partition type as Key and partition
        path as Value for the partitions created by this method.

    """
    LOG.debug("Starting to partition the disk device: %(dev)s "
              "for node %(node)s",
              {'dev': dev, 'node': node_uuid})
    part_template = dev + '-part%d'
    part_dict = {}

    # For uefi localboot, switch partition table to gpt and create the efi
    # system partition as the first partition.
    if boot_mode == "uefi" and boot_option == "local":
        dp = disk_partitioner.DiskPartitioner(dev, disk_label="gpt")
        part_num = dp.add_partition(CONF.disk_utils.efi_system_partition_size,
                                    fs_type='fat32',
                                    bootable=True)
        part_dict['efi system partition'] = part_template % part_num
    else:
        dp = disk_partitioner.DiskPartitioner(dev)

    if ephemeral_mb:
        LOG.debug("Add ephemeral partition (%(size)d MB) to device: %(dev)s "
                  "for node %(node)s",
                  {'dev': dev, 'size': ephemeral_mb, 'node': node_uuid})
        part_num = dp.add_partition(ephemeral_mb)
        part_dict['ephemeral'] = part_template % part_num
    if swap_mb:
        LOG.debug("Add Swap partition (%(size)d MB) to device: %(dev)s "
                  "for node %(node)s",
                  {'dev': dev, 'size': swap_mb, 'node': node_uuid})
        part_num = dp.add_partition(swap_mb, fs_type='linux-swap')
        part_dict['swap'] = part_template % part_num
    if configdrive_mb:
        LOG.debug("Add config drive partition (%(size)d MB) to device: "
                  "%(dev)s for node %(node)s",
                  {'dev': dev, 'size': configdrive_mb, 'node': node_uuid})
        part_num = dp.add_partition(configdrive_mb)
        part_dict['configdrive'] = part_template % part_num

    # NOTE(lucasagomes): Make the root partition the last partition. This
    # enables tools like cloud-init's growroot utility to expand the root
    # partition until the end of the disk.
    LOG.debug("Add root partition (%(size)d MB) to device: %(dev)s "
              "for node %(node)s",
              {'dev': dev, 'size': root_mb, 'node': node_uuid})
    part_num = dp.add_partition(root_mb, bootable=(boot_option == "local" and
                                                   boot_mode == "bios"))
    part_dict['root'] = part_template % part_num

    if commit:
        # write to the disk
        dp.commit()
    return part_dict


def is_block_device(dev):
    """Check whether a device is block or not."""
    attempts = CONF.disk_utils.iscsi_verify_attempts
    for attempt in range(attempts):
        try:
            s = os.stat(dev)
        except OSError as e:
            LOG.debug("Unable to stat device %(dev)s. Attempt %(attempt)d "
                      "out of %(total)d. Error: %(err)s",
                      {"dev": dev, "attempt": attempt + 1,
                       "total": attempts, "err": e})
            time.sleep(1)
        else:
            return stat.S_ISBLK(s.st_mode)
    msg = _("Unable to stat device %(dev)s after attempting to verify "
            "%(attempts)d times.") % {'dev': dev, 'attempts': attempts}
    LOG.error(msg)
    raise exception.InstanceDeployFailure(msg)


def dd(src, dst):
    """Execute dd from src to dst."""
    utils.dd(src, dst, 'bs=%s' % CONF.disk_utils.dd_block_size, 'oflag=direct')


def qemu_img_info(path):
    """Return an object containing the parsed output from qemu-img info."""
    if not os.path.exists(path):
        return imageutils.QemuImgInfo()

    out, err = utils.execute('env', 'LC_ALL=C', 'LANG=C',
                             'qemu-img', 'info', path)
    return imageutils.QemuImgInfo(out)


def convert_image(source, dest, out_format, run_as_root=False):
    """Convert image to other format."""
    cmd = ('qemu-img', 'convert', '-O', out_format, source, dest)
    utils.execute(*cmd, run_as_root=run_as_root)


def populate_image(src, dst):
    data = qemu_img_info(src)
    if data.file_format == 'raw':
        dd(src, dst)
    else:
        convert_image(src, dst, 'raw', True)


# TODO(rameshg87): Remove this one-line method and use utils.mkfs
# directly.
def mkfs(fs, dev, label=None):
    """Execute mkfs on a device."""
    utils.mkfs(fs, dev, label)


def block_uuid(dev):
    """Get UUID of a block device."""
    out, _err = utils.execute('blkid', '-s', 'UUID', '-o', 'value', dev,
                              run_as_root=True,
                              check_exit_code=[0])
    return out.strip()


def get_image_mb(image_path, virtual_size=True):
    """Get size of an image in Megabyte."""
    mb = 1024 * 1024
    if not virtual_size:
        image_byte = os.path.getsize(image_path)
    else:
        data = qemu_img_info(image_path)
        image_byte = data.virtual_size

    # round up size to MB
    image_mb = int((image_byte + mb - 1) / mb)
    return image_mb


def get_dev_block_size(dev):
    """Get the device size in 512 byte sectors."""
    block_sz, cmderr = utils.execute('blockdev', '--getsz', dev,
                                     run_as_root=True, check_exit_code=[0])
    return int(block_sz)


def destroy_disk_metadata(dev, node_uuid):
    """Destroy metadata structures on node's disk.

       Ensure that node's disk appears to be blank without zeroing the entire
       drive. To do this we will zero the first 18KiB to clear MBR / GPT data
       and the last 18KiB to clear GPT and other metadata like LVM, veritas,
       MDADM, DMRAID, etc.
    """
    # NOTE(NobodyCam): This is needed to work around bug:
    # https://bugs.launchpad.net/ironic/+bug/1317647
    LOG.debug("Start destroy disk metadata for node %(node)s.",
              {'node': node_uuid})
    try:
        utils.dd('/dev/zero', dev, 'bs=512', 'count=36')
    except processutils.ProcessExecutionError as err:
        with excutils.save_and_reraise_exception():
            LOG.error(_LE("Failed to erase beginning of disk for node "
                          "%(node)s. Command: %(command)s. Error: %(error)s."),
                      {'node': node_uuid,
                       'command': err.cmd,
                       'error': err.stderr})

    # now wipe the end of the disk.
    # get end of disk seek value
    try:
        block_sz = get_dev_block_size(dev)
    except processutils.ProcessExecutionError as err:
        with excutils.save_and_reraise_exception():
            LOG.error(_LE("Failed to get disk block count for node %(node)s. "
                          "Command: %(command)s. Error: %(error)s."),
                      {'node': node_uuid,
                       'command': err.cmd,
                       'error': err.stderr})
    else:
        seek_value = block_sz - 36
        try:
            utils.dd('/dev/zero', dev, 'bs=512', 'count=36',
                     'seek=%d' % seek_value)
        except processutils.ProcessExecutionError as err:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Failed to erase the end of the disk on node "
                              "%(node)s. Command: %(command)s. "
                              "Error: %(error)s."),
                          {'node': node_uuid,
                           'command': err.cmd,
                           'error': err.stderr})
    LOG.info(_LI("Disk metadata on %(dev)s successfully destroyed for node "
                 "%(node)s"), {'dev': dev, 'node': node_uuid})


def _get_configdrive(configdrive, node_uuid, tempdir=None):
    """Get the information about size and location of the configdrive.

    :param configdrive: Base64 encoded Gzipped configdrive content or
        configdrive HTTP URL.
    :param node_uuid: Node's uuid. Used for logging.
    :param tempdir: temporary directory for the temporary configdrive file
    :raises: InstanceDeployFailure if it can't download or decode the
       config drive.
    :returns: A tuple with the size in MiB and path to the uncompressed
        configdrive file.

    """
    # Check if the configdrive option is a HTTP URL or the content directly
    is_url = utils.is_http_url(configdrive)
    if is_url:
        try:
            data = requests.get(configdrive).content
        except requests.exceptions.RequestException as e:
            raise exception.InstanceDeployFailure(
                _("Can't download the configdrive content for node %(node)s "
                  "from '%(url)s'. Reason: %(reason)s") %
                {'node': node_uuid, 'url': configdrive, 'reason': e})
    else:
        data = configdrive

    try:
        data = six.BytesIO(base64.b64decode(data))
    except TypeError:
        error_msg = (_('Config drive for node %s is not base64 encoded '
                       'or the content is malformed.') % node_uuid)
        if is_url:
            error_msg += _(' Downloaded from "%s".') % configdrive
        raise exception.InstanceDeployFailure(error_msg)

    configdrive_file = tempfile.NamedTemporaryFile(delete=False,
                                                   prefix='configdrive',
                                                   dir=tempdir)
    configdrive_mb = 0
    with gzip.GzipFile('configdrive', 'rb', fileobj=data) as gunzipped:
        try:
            shutil.copyfileobj(gunzipped, configdrive_file)
        except EnvironmentError as e:
            # Delete the created file
            utils.unlink_without_raise(configdrive_file.name)
            raise exception.InstanceDeployFailure(
                _('Encountered error while decompressing and writing '
                  'config drive for node %(node)s. Error: %(exc)s') %
                {'node': node_uuid, 'exc': e})
        else:
            # Get the file size and convert to MiB
            configdrive_file.seek(0, os.SEEK_END)
            bytes_ = configdrive_file.tell()
            configdrive_mb = int(math.ceil(float(bytes_) / units.Mi))
        finally:
            configdrive_file.close()

        return (configdrive_mb, configdrive_file.name)


def work_on_disk(dev, root_mb, swap_mb, ephemeral_mb, ephemeral_format,
                 image_path, node_uuid, preserve_ephemeral=False,
                 configdrive=None, boot_option="netboot", boot_mode="bios",
                 tempdir=None):
    """Create partitions and copy an image to the root partition.

    :param dev: Path for the device to work on.
    :param root_mb: Size of the root partition in megabytes.
    :param swap_mb: Size of the swap partition in megabytes.
    :param ephemeral_mb: Size of the ephemeral partition in megabytes. If 0,
        no ephemeral partition will be created.
    :param ephemeral_format: The type of file system to format the ephemeral
        partition.
    :param image_path: Path for the instance's disk image.
    :param node_uuid: node's uuid. Used for logging.
    :param preserve_ephemeral: If True, no filesystem is written to the
        ephemeral block device, preserving whatever content it had (if the
        partition table has not changed).
    :param configdrive: Optional. Base64 encoded Gzipped configdrive content
                        or configdrive HTTP URL.
    :param boot_option: Can be "local" or "netboot". "netboot" by default.
    :param boot_mode: Can be "bios" or "uefi". "bios" by default.
    :param tempdir: A temporary directory
    :returns: a dictionary containing the following keys:
        'root uuid': UUID of root partition
        'efi system partition uuid': UUID of the uefi system partition
                                     (if boot mode is uefi).
        NOTE: If key exists but value is None, it means partition doesn't
              exist.
    """
    # the only way for preserve_ephemeral to be set to true is if we are
    # rebuilding an instance with --preserve_ephemeral.
    commit = not preserve_ephemeral
    # now if we are committing the changes to disk clean first.
    if commit:
        destroy_disk_metadata(dev, node_uuid)

    try:
        # If requested, get the configdrive file and determine the size
        # of the configdrive partition
        configdrive_mb = 0
        configdrive_file = None
        if configdrive:
            configdrive_mb, configdrive_file = _get_configdrive(
                configdrive, node_uuid, tempdir=tempdir)

        part_dict = make_partitions(dev, root_mb, swap_mb, ephemeral_mb,
                                    configdrive_mb, node_uuid,
                                    commit=commit,
                                    boot_option=boot_option,
                                    boot_mode=boot_mode)
        LOG.info(_LI("Successfully completed the disk device"
                     " %(dev)s partitioning for node %(node)s"),
                 {'dev': dev, "node": node_uuid})

        ephemeral_part = part_dict.get('ephemeral')
        swap_part = part_dict.get('swap')
        configdrive_part = part_dict.get('configdrive')
        root_part = part_dict.get('root')

        if not is_block_device(root_part):
            raise exception.InstanceDeployFailure(
                _("Root device '%s' not found") % root_part)

        for part in ('swap', 'ephemeral', 'configdrive',
                     'efi system partition'):
            part_device = part_dict.get(part)
            LOG.debug("Checking for %(part)s device (%(dev)s) on node "
                      "%(node)s.", {'part': part, 'dev': part_device,
                                    'node': node_uuid})
            if part_device and not is_block_device(part_device):
                raise exception.InstanceDeployFailure(
                    _("'%(partition)s' device '%(part_device)s' not found") %
                    {'partition': part, 'part_device': part_device})

        # If it's a uefi localboot, then we have created the efi system
        # partition.  Create a fat filesystem on it.
        if boot_mode == "uefi" and boot_option == "local":
            efi_system_part = part_dict.get('efi system partition')
            mkfs(dev=efi_system_part, fs='vfat', label='efi-part')

        if configdrive_part:
            # Copy the configdrive content to the configdrive partition
            dd(configdrive_file, configdrive_part)
            LOG.info(_LI("Configdrive for node %(node)s successfully copied "
                         "onto partition %(partition)s"),
                     {'node': node_uuid, 'partition': configdrive_part})

    finally:
        # If the configdrive was requested make sure we delete the file
        # after copying the content to the partition
        if configdrive_file:
            utils.unlink_without_raise(configdrive_file)

    populate_image(image_path, root_part)
    LOG.info(_LI("Image for %(node)s successfully populated"),
             {'node': node_uuid})

    if swap_part:
        mkfs(dev=swap_part, fs='swap', label='swap1')
        LOG.info(_LI("Swap partition %(swap)s successfully formatted "
                     "for node %(node)s"),
                 {'swap': swap_part, 'node': node_uuid})

    if ephemeral_part and not preserve_ephemeral:
        mkfs(dev=ephemeral_part, fs=ephemeral_format, label="ephemeral0")
        LOG.info(_LI("Ephemeral partition %(ephemeral)s successfully "
                     "formatted for node %(node)s"),
                 {'ephemeral': ephemeral_part, 'node': node_uuid})

    uuids_to_return = {
        'root uuid': root_part,
        'efi system partition uuid': part_dict.get('efi system partition')
    }

    try:
        for part, part_dev in uuids_to_return.items():
            if part_dev:
                uuids_to_return[part] = block_uuid(part_dev)

    except processutils.ProcessExecutionError:
        with excutils.save_and_reraise_exception():
            LOG.error(_LE("Failed to detect %s"), part)

    return uuids_to_return
