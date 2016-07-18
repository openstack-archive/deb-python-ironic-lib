# Copyright 2016 Rackspace Hosting
# All Rights Reserved
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

import socket

import mock
from oslotest import base as test_base

from ironic_lib import metrics_statsd


class TestStatsdMetricLogger(test_base.BaseTestCase):
    def setUp(self):
        super(TestStatsdMetricLogger, self).setUp()
        self.ml = metrics_statsd.StatsdMetricLogger('prefix', '.', 'test-host',
                                                    4321)

    def test_init(self):
        self.assertEqual(self.ml._host, 'test-host')
        self.assertEqual(self.ml._port, 4321)
        self.assertEqual(self.ml._target, ('test-host', 4321))

    @mock.patch('ironic_lib.metrics_statsd.StatsdMetricLogger._send',
                autospec=True)
    def test_gauge(self, mock_send):
        self.ml._gauge('metric', 10)
        mock_send.assert_called_once_with(self.ml, 'metric', 10, 'g')

    @mock.patch('ironic_lib.metrics_statsd.StatsdMetricLogger._send',
                autospec=True)
    def test_counter(self, mock_send):
        self.ml._counter('metric', 10)
        mock_send.assert_called_once_with(self.ml, 'metric', 10, 'c',
                                          sample_rate=None)
        mock_send.reset_mock()

        self.ml._counter('metric', 10, sample_rate=1.0)
        mock_send.assert_called_once_with(self.ml, 'metric', 10, 'c',
                                          sample_rate=1.0)

    @mock.patch('ironic_lib.metrics_statsd.StatsdMetricLogger._send',
                autospec=True)
    def test_timer(self, mock_send):
        self.ml._timer('metric', 10)
        mock_send.assert_called_once_with(self.ml, 'metric', 10, 'ms')

    @mock.patch('socket.socket')
    def test_open_socket(self, mock_socket_constructor):
        self.ml._open_socket()
        mock_socket_constructor.assert_called_once_with(
            socket.AF_INET,
            socket.SOCK_DGRAM)

    @mock.patch('socket.socket')
    def test_send(self, mock_socket_constructor):
        mock_socket = mock.Mock()
        mock_socket_constructor.return_value = mock_socket

        self.ml._send('part1.part2', 2, 'type')
        mock_socket.sendto.assert_called_once_with(
            'part1.part2:2|type',
            ('test-host', 4321))
        mock_socket.close.assert_called_once_with()
        mock_socket.reset_mock()

        self.ml._send('part1.part2', 3.14159, 'type')
        mock_socket.sendto.assert_called_once_with(
            'part1.part2:3.14159|type',
            ('test-host', 4321))
        mock_socket.close.assert_called_once_with()
        mock_socket.reset_mock()

        self.ml._send('part1.part2', 5, 'type')
        mock_socket.sendto.assert_called_once_with(
            'part1.part2:5|type',
            ('test-host', 4321))
        mock_socket.close.assert_called_once_with()
        mock_socket.reset_mock()

        self.ml._send('part1.part2', 5, 'type', sample_rate=0.5)
        mock_socket.sendto.assert_called_once_with(
            'part1.part2:5|type@0.5',
            ('test-host', 4321))
        mock_socket.close.assert_called_once_with()
