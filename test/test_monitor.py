from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
import unittest
from configparser import ConfigParser
from pathlib import Path
from threading import Event
from unittest.mock import patch

# L'ambiente di sviluppo può non avere ancora installato requirements.txt.
try:
    import paho.mqtt.client  # noqa: F401
except ModuleNotFoundError:
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_client = types.ModuleType("paho.mqtt.client")
    paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    paho_client.MQTT_ERR_SUCCESS = 0
    paho_client.Client = object
    paho.mqtt = paho_mqtt
    paho_mqtt.client = paho_client
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = paho_mqtt
    sys.modules["paho.mqtt.client"] = paho_client

try:
    import sdnotify  # noqa: F401
except ModuleNotFoundError:
    sdnotify = types.ModuleType("sdnotify")
    sdnotify.SystemdNotifier = lambda: types.SimpleNamespace(notify=lambda message: None)
    sys.modules["sdnotify"] = sdnotify

import platform_monitor_2_mqtt as app


class ReasonCode:
    def __init__(self, value: int):
        self.value = value


class PublishInfo:
    def __init__(self, rc: int = 0):
        self.rc = rc
        self.waited = False
        self.timeout = None
        self.published = True

    def wait_for_publish(self, timeout=None):
        self.waited = True
        self.timeout = timeout

    def is_published(self):
        return self.published


class FakeClient:
    connect_reason = 0
    connect_exception = None
    invoke_connect_callback = True
    publish_rc = 0
    publish_confirmed = True

    def __init__(self, callback_api_version):
        self.callback_api_version = callback_api_version
        self.on_connect = None
        self.on_publish = None
        self.on_disconnect = None
        self.published = []
        self.publish_infos = []
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.will = None
        self.reconnect_delays = None

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect_delays = (min_delay, max_delay)

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def tls_set(self, **kwargs):
        self.tls_kwargs = kwargs

    def connect(self, hostname, port, keepalive):
        if self.connect_exception:
            raise self.connect_exception
        self.connection = (hostname, port, keepalive)

    def loop_start(self):
        self.loop_started = True
        if self.on_connect is not None and self.invoke_connect_callback:
            self.on_connect(self, None, {}, ReasonCode(self.connect_reason), None)

    def publish(self, topic, payload, qos, retain):
        info = PublishInfo(self.publish_rc)
        info.published = self.publish_confirmed
        self.published.append((topic, payload, qos, retain))
        self.publish_infos.append(info)
        return info

    def disconnect(self):
        self.disconnected = True
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, {}, ReasonCode(0), None)

    def loop_stop(self):
        self.loop_stopped = True


class GoodModule:
    def __init__(self):
        self.collect_count = 0
        self.closed = False

    def collect(self):
        self.collect_count += 1

    def getData(self):
        return {"value": 42}

    def close(self):
        self.closed = True


class BrokenModule:
    def collect(self):
        raise RuntimeError("collector failed")

    def getData(self):
        return {"unreachable": True}


class BackgroundModule(GoodModule):
    def __init__(self, idle=True):
        super().__init__()
        self.idle = idle
        self.wait_timeout = None

    def wait_for_idle(self, timeout):
        self.wait_timeout = timeout
        return self.idle


def build_config() -> ConfigParser:
    config = ConfigParser(interpolation=None)
    config.optionxform = str
    config["General"] = {"save_json": "false", "one_shot_wait_seconds": "1"}
    config["Daemon"] = {"enabled": "true", "interval_in_minutes": "1"}
    config["MQTT"] = {
        "hostname": "broker.local",
        "port": "1883",
        "keepalive": "60",
        "tls": "false",
        "qos": "1",
        "connect_timeout_seconds": "1",
        "publish_timeout_seconds": "2",
        "reconnect_min_delay_seconds": "2",
        "reconnect_max_delay_seconds": "30",
    }
    config["MQTT topic"] = {
        "base_topic": "home/nodes",
        "sensor_name": "test-node",
    }
    config["Modules"] = {}
    return config


class MonitorTests(unittest.TestCase):
    def setUp(self):
        app.config = build_config()
        app.logger = logging.getLogger("platformMonitor.tests")
        app.logger.handlers.clear()
        app.logger.addHandler(logging.NullHandler())
        FakeClient.connect_reason = 0
        FakeClient.connect_exception = None
        FakeClient.invoke_connect_callback = True
        FakeClient.publish_rc = 0
        FakeClient.publish_confirmed = True
        self.client_patch = patch.object(app.mqtt, "Client", side_effect=FakeClient)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()

    def new_monitor(self):
        return app.Monitor2MQTT(Event())

    def test_connect_uses_paho_v2_and_publishes_online_immediately(self):
        monitor = self.new_monitor()
        self.assertTrue(monitor.connect())
        client = monitor._client
        self.assertEqual(client.callback_api_version, app.mqtt.CallbackAPIVersion.VERSION2)
        self.assertEqual(client.reconnect_delays, (2, 30))
        self.assertEqual(client.will[2], 1)
        topics = [item[0] for item in client.published]
        self.assertIn("home/nodes/test-node/availability", topics)
        self.assertIn("home/nodes/test-node/timestamp", topics)
        monitor.close()

    def test_rejected_connection_returns_false_without_process_exit(self):
        FakeClient.connect_reason = 5
        monitor = self.new_monitor()
        self.assertFalse(monitor.connect())
        self.assertTrue(monitor._client.loop_stopped)

    def test_socket_connection_error_returns_false(self):
        FakeClient.connect_exception = OSError("unreachable")
        monitor = self.new_monitor()
        self.assertFalse(monitor.connect())

    def test_connect_timeout_stops_network_loop(self):
        FakeClient.invoke_connect_callback = False
        monitor = self.new_monitor()
        monitor._connect_timeout = 0.01
        self.assertFalse(monitor.connect())
        self.assertTrue(monitor._client.loop_stopped)

    def test_publish_error_is_propagated(self):
        monitor = self.new_monitor()
        self.assertTrue(monitor.connect())
        FakeClient.publish_rc = 4
        with patch.object(monitor, "_collect_data", return_value={"value": 1}):
            self.assertFalse(monitor.execute())
        monitor.close()

    def test_publish_confirmation_timeout_is_propagated(self):
        monitor = self.new_monitor()
        self.assertTrue(monitor.connect())
        FakeClient.publish_confirmed = False
        with patch.object(monitor, "_collect_data", return_value={"value": 1}):
            self.assertFalse(monitor.execute())
        monitor.close()

    def test_execute_checks_qos_and_waits_for_confirmation(self):
        monitor = self.new_monitor()
        self.assertTrue(monitor.connect())
        client = monitor._client
        client.published.clear()
        client.publish_infos.clear()
        with patch.object(monitor, "_collect_data", return_value={"value": 1}):
            self.assertTrue(monitor.execute())
        self.assertEqual(len(client.published), 3)
        self.assertTrue(all(item[2] == 1 for item in client.published))
        self.assertTrue(all(info.waited for info in client.publish_infos))
        monitor.close()

    def test_graceful_close_publishes_offline_and_stops_network_loop(self):
        monitor = self.new_monitor()
        self.assertTrue(monitor.connect())
        client = monitor._client
        client.published.clear()
        monitor.close(graceful=True)
        self.assertIn(
            ("home/nodes/test-node/availability", "offline", 1, True),
            client.published,
        )
        self.assertTrue(client.disconnected)
        self.assertTrue(client.loop_stopped)
        monitor.close(graceful=True)  # idempotente

    def test_reconnect_callback_republishes_online(self):
        monitor = self.new_monitor()
        self.assertTrue(monitor.connect())
        client = monitor._client
        client.published.clear()
        monitor._on_disconnect(client, None, {}, ReasonCode(7), None)
        monitor._on_connect(client, None, {}, ReasonCode(0), None)
        topics_and_payloads = [(item[0], item[1]) for item in client.published]
        self.assertIn(("home/nodes/test-node/availability", "online"), topics_and_payloads)
        self.assertTrue(monitor._connected)
        monitor.close()

    def test_collect_isolates_module_exceptions(self):
        app.config["Modules"] = {
            "good": "unused,Unused",
            "broken": "unused,Unused",
        }
        monitor = self.new_monitor()
        monitor._modules = {"good": GoodModule(), "broken": BrokenModule()}
        data = monitor._collect_data()
        self.assertEqual(data["good"], {"value": 42})
        self.assertEqual(data["broken"], {})

    def test_wait_and_close_background_modules(self):
        monitor = self.new_monitor()
        module = BackgroundModule(idle=True)
        monitor._modules = {"background": module}
        self.assertTrue(monitor.wait_for_background_modules(1))
        monitor._close_modules()
        self.assertTrue(module.closed)

    def test_run_executes_once_then_stops(self):
        stop_event = Event()
        stop_event.set()
        monitor = app.Monitor2MQTT(stop_event)
        with patch.object(monitor, "execute") as execute:
            monitor.run()
        execute.assert_called_once_with()


class CredentialTests(unittest.TestCase):
    """Precedenza: credenziale systemd → variabili d'ambiente → monitor.ini."""

    ENV_KEYS = ("CREDENTIALS_DIRECTORY", "MQTT_USERNAME", "MQTT_PASSWORD")

    def setUp(self):
        app.config = build_config()
        app.logger = logging.getLogger("platformMonitor.tests")
        app.logger.handlers.clear()
        app.logger.addHandler(logging.NullHandler())
        self.client_patch = patch.object(app.mqtt, "Client", side_effect=FakeClient)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cred_dir = Path(self.tmp.name)

        saved = {key: os.environ.pop(key, None) for key in self.ENV_KEYS}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def write_credential(self, name, value):
        (self.cred_dir / name).write_text(value, encoding="utf-8")
        os.environ["CREDENTIALS_DIRECTORY"] = str(self.cred_dir)

    def credentials(self):
        return getattr(app.Monitor2MQTT(Event())._client, "credentials", None)

    def test_ini_is_used_when_nothing_else_is_available(self):
        app.config["MQTT"]["username"] = "ini-user"
        app.config["MQTT"]["password"] = "ini-pass"
        self.assertEqual(self.credentials(), ("ini-user", "ini-pass"))

    def test_environment_overrides_ini(self):
        app.config["MQTT"]["username"] = "ini-user"
        app.config["MQTT"]["password"] = "ini-pass"
        os.environ["MQTT_USERNAME"] = "env-user"
        os.environ["MQTT_PASSWORD"] = "env-pass"
        self.assertEqual(self.credentials(), ("env-user", "env-pass"))

    def test_systemd_credential_overrides_environment_and_ini(self):
        app.config["MQTT"]["username"] = "ini-user"
        app.config["MQTT"]["password"] = "ini-pass"
        os.environ["MQTT_USERNAME"] = "env-user"
        os.environ["MQTT_PASSWORD"] = "env-pass"
        self.write_credential("mqtt_username", "cred-user")
        self.write_credential("mqtt_password", "cred-pass")
        self.assertEqual(self.credentials(), ("cred-user", "cred-pass"))

    def test_precedence_is_evaluated_per_field(self):
        app.config["MQTT"]["username"] = "ini-user"
        app.config["MQTT"]["password"] = "ini-pass"
        os.environ["MQTT_PASSWORD"] = "env-pass"
        self.write_credential("mqtt_username", "cred-user")
        self.assertEqual(self.credentials(), ("cred-user", "env-pass"))

    def test_missing_credential_falls_back_to_environment(self):
        os.environ["CREDENTIALS_DIRECTORY"] = str(self.cred_dir)
        os.environ["MQTT_USERNAME"] = "env-user"
        os.environ["MQTT_PASSWORD"] = "env-pass"
        self.assertEqual(self.credentials(), ("env-user", "env-pass"))

    def test_empty_credential_falls_back(self):
        self.write_credential("mqtt_username", "")
        self.write_credential("mqtt_password", "\n")
        os.environ["MQTT_USERNAME"] = "env-user"
        os.environ["MQTT_PASSWORD"] = "env-pass"
        self.assertEqual(self.credentials(), ("env-user", "env-pass"))

    def test_only_final_newline_is_stripped(self):
        self.write_credential("mqtt_username", "user")
        self.write_credential("mqtt_password", "  pa ss#=\"$x  \r\n")
        self.assertEqual(self.credentials(), ("user", "  pa ss#=\"$x  "))

    def test_no_credentials_anywhere_means_anonymous_connection(self):
        self.assertIsNone(self.credentials())

    def test_unreadable_credential_directory_falls_back(self):
        os.environ["CREDENTIALS_DIRECTORY"] = str(self.cred_dir / "inesistente")
        os.environ["MQTT_USERNAME"] = "env-user"
        self.assertEqual(self.credentials(), ("env-user", None))

    def test_invalid_credential_names_are_rejected(self):
        os.environ["CREDENTIALS_DIRECTORY"] = str(self.cred_dir)
        for name in ("../x", "a/b", ".hidden", ""):
            self.assertIsNone(app._read_systemd_credential(name))

    def test_secret_values_are_never_logged(self):
        self.write_credential("mqtt_username", "cred-user")
        self.write_credential("mqtt_password", "super-secret-value")
        with self.assertLogs(app.logger, level="INFO") as captured:
            self.credentials()
        output = "\n".join(captured.output)
        self.assertNotIn("super-secret-value", output)
        self.assertNotIn("cred-user", output)
        self.assertIn("systemd-credential", output)


if __name__ == "__main__":
    unittest.main()
