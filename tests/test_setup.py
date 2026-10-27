"""Non-invasive installer tests; no systemctl, printer, or network required."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
spec = importlib.util.spec_from_file_location("revive_setup", MODULE_PATH)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        extras = root / "klipper" / "klippy" / "extras"
        config = root / "printer_data" / "config"
        extras.mkdir(parents=True)
        config.mkdir(parents=True)
        self.printer = config / "printer.cfg"
        self.moon = config / "moonraker.conf"
        self.printer.write_text("[printer]\nkinematics: cartesian\n", encoding="utf-8")
        self.moon.write_text("[server]\nhost: 0.0.0.0\n", encoding="utf-8")
        self.p = {
            "repo": MODULE_PATH.parent.parent,
            "source": MODULE_PATH.parent.parent / "klipper_extras" / setup.EXTRA_NAME,
            "extra": extras / setup.EXTRA_NAME,
            "printer": self.printer,
            "moonraker": self.moon,
        }

    def test_install_and_uninstall(self):
        self.assertTrue(setup.install(self.p))
        self.assertTrue(self.p["extra"].is_symlink())
        self.assertEqual(self.p["extra"].resolve(), self.p["source"].resolve())
        self.assertIn("[include klipper_revive.cfg]", self.printer.read_text())
        self.assertIn("[include klipper_revive_update.cfg]", self.moon.read_text())
        updater = (self.moon.parent / setup.MOONRAKER_NAME).read_text()
        for key in ("type: git_repo", "primary_branch: master", "managed_services: klipper"):
            self.assertIn(key, updater)
        self.assertFalse(setup.install(self.p))
        self.assertEqual(self.printer.read_text().count("[include klipper_revive.cfg]"), 1)
        self.assertTrue(setup.uninstall(self.p))
        self.assertFalse(self.p["extra"].exists())
        self.assertNotIn("[include klipper_revive.cfg]", self.printer.read_text())
        self.assertNotIn("[include klipper_revive_update.cfg]", self.moon.read_text())
        self.assertFalse((self.moon.parent / setup.MOONRAKER_NAME).exists())
        self.assertFalse(setup.uninstall(self.p))

    SAVE_CONFIG_BLOCK = (
        "\n#*# <---------------------- SAVE_CONFIG ---------------------->\n"
        "#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n"
        "#*#\n"
        "#*# [probe]\n"
        "#*# z_offset = 1.235\n"
        "#*#\n"
        "#*# [bed_mesh default]\n"
        "#*# version = 1\n"
    )

    def test_save_config_insert_before_autosave(self):
        base = "[probe]\npin: ^PA0\n" + self.SAVE_CONFIG_BLOCK
        self.printer.write_text(base, encoding="utf-8")
        self.assertTrue(setup.install(self.p))
        installed = self.printer.read_text()
        self.assertIn("[include klipper_revive.cfg]", installed)
        self.assertLess(installed.index("[include klipper_revive.cfg]"),
                        installed.index("SAVE_CONFIG"))
        self.assertTrue(installed.endswith(self.SAVE_CONFIG_BLOCK.lstrip("\n")))
        self.assertEqual(installed.count("[include klipper_revive.cfg]"), 1)
        self.assertFalse(setup.install(self.p))
        self.assertEqual(installed, self.printer.read_text())
        self.assertTrue(setup.uninstall(self.p))
        self.assertNotIn("[include klipper_revive.cfg]", self.printer.read_text())
        self.assertIn("#*# z_offset = 1.235", self.printer.read_text())

    def test_repair_legacy_include_below_save_config(self):
        # Reproduces the live printer halt: Klipper rejects SAVE_CONFIG
        # because the installer appended a non-#*# line after the header.
        broken = (
            "[probe]\npin: ^PA0\n" + self.SAVE_CONFIG_BLOCK
            + "\n[include klipper_revive.cfg]\n"
        )
        self.printer.write_text(broken, encoding="utf-8")
        self.assertTrue(setup.install(self.p))
        repaired = self.printer.read_text()
        self.assertLess(repaired.index("[include klipper_revive.cfg]"),
                        repaired.index("SAVE_CONFIG"))
        self.assertEqual(repaired.count("[include klipper_revive.cfg]"), 1)
        self.assertIn(self.SAVE_CONFIG_BLOCK, repaired)
        self.assertEqual(repaired.split("SAVE_CONFIG", 1)[1]
                         .count("[include klipper_revive.cfg]"), 0)
        self.assertFalse(setup.install(self.p))

    def test_existing_include_above_autosave_untouched(self):
        valid = (
            "[probe]\npin: ^PA0\n\n[include klipper_revive.cfg]\n"
            + self.SAVE_CONFIG_BLOCK
        )
        self.printer.write_text(valid, encoding="utf-8")
        self.assertTrue(setup.install(self.p))
        self.assertEqual(self.printer.read_text(), valid)

    def test_ambiguous_autosave_headers_refused(self):
        invalid = (
            "[probe]\npin: ^PA0\n" + self.SAVE_CONFIG_BLOCK
            + self.SAVE_CONFIG_BLOCK
        )
        self.printer.write_text(invalid, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Multiple SAVE_CONFIG"):
            setup.install(self.p)
        self.assertEqual(self.printer.read_text(), invalid)

    def test_upgrade_old_moonraker_main_branch_to_master(self):
        old = setup.updater_text(self.p["repo"]).replace(
            "primary_branch: master", "primary_branch: main")
        updater = self.moon.parent / setup.MOONRAKER_NAME
        updater.write_text(old, encoding="utf-8")
        self.assertTrue(setup.install(self.p))
        text = updater.read_text(encoding="utf-8")
        self.assertIn("primary_branch: master", text)
        self.assertNotIn("primary_branch: main", text)
        self.assertFalse(setup.install(self.p))

    def test_foreign_module_refused_without_changes(self):
        self.p["extra"].write_text("# foreign module\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Refusing"):
            setup.install(self.p)
        self.assertNotIn("include", self.printer.read_text())
        self.assertFalse((self.moon.parent / setup.MOONRAKER_NAME).exists())

    def test_unmanaged_snippet_refused(self):
        (self.moon.parent / setup.MOONRAKER_NAME).write_text(
            "[update_manager klipper-revive]\norigin: other\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "unmanaged"):
            setup.install(self.p)
        self.assertFalse(self.p["extra"].exists())

    def test_user_config_preserved(self):
        self.assertTrue(setup.install(self.p))
        self.assertIn("kinematics: cartesian", self.printer.read_text())
        self.assertIn("host: 0.0.0.0", self.moon.read_text())
        self.assertTrue((self.printer.parent / "printer.cfg.klipper-revive.bak").exists())

    def test_moonraker_state_restart_safety(self):
        import io
        import json

        def check(klippy_state, print_state=None):
            def fake_urlopen(url, timeout):
                if url.endswith("/server/info"):
                    data = {"result": {"klippy_state": klippy_state}}
                else:
                    self.assertTrue(url.endswith("/printer/objects/query?print_stats"))
                    data = {"result": {"status": {
                        "print_stats": {"state": print_state}}}}
                return io.BytesIO(json.dumps(data).encode("utf-8"))

            with mock.patch.object(setup, "urlopen", side_effect=fake_urlopen):
                return setup.idle_from_moonraker("http://127.0.0.1:7125")

        self.assertTrue(check("error")[0])
        for state in ("startup", "disconnected", "shutdown", "unknown"):
            # An emergency shutdown must NEVER be automatically restarted.
            self.assertFalse(check(state)[0], state)
        for state in ("standby", "complete", "cancelled", "error"):
            self.assertTrue(check("ready", state)[0], state)
        for state in ("printing", "paused", "unknown"):
            self.assertFalse(check("ready", state)[0], state)

    def test_moonraker_unreachable_fails_closed(self):
        with mock.patch.object(setup, "urlopen", side_effect=OSError("offline")):
            safe, reason = setup.idle_from_moonraker("http://127.0.0.1:7125")
        self.assertFalse(safe)
        self.assertIn("unavailable", reason)

    def test_uninstall_retries_restart_when_already_removed(self):
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        # Re-running uninstall after a previously skipped service restart
        # must still unload the Python module from the live Klipper process.
        with mock.patch.object(setup, "idle_from_moonraker",
                               return_value=(True, "print_stats: standby")):
            with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch.object(setup.subprocess, "run",
                                       return_value=SimpleNamespace(returncode=0)) as run:
                    setup.restart_if_safe(args, False, action="uninstall")
                    self.assertEqual(run.call_count, 3)
                    self.assertEqual(run.call_args_list[0].args[0],
                                     ["sudo", "-n", "-v"])
                    self.assertEqual(run.call_args_list[1].args[0],
                                     ["sudo", "-n", "systemctl", "restart", "klipper"])
                    self.assertEqual(run.call_args_list[2].args[0],
                                     ["sudo", "-n", "systemctl", "restart", "moonraker"])

    def test_interactive_password_prompt_install_and_uninstall(self):
        import io
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        for action, changed in (("install", True), ("uninstall", False)):
            # First sudo -n -v is rejected, then sudo -v prompts on
            # /dev/tty even when installer stdin is a shell-script pipe.
            terminal = io.StringIO()
            results = [SimpleNamespace(returncode=n) for n in (1, 0, 0, 0)]
            with self.subTest(action=action):
                with mock.patch.object(setup, "idle_from_moonraker",
                                       return_value=(True, "standby")) as idle:
                    with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tool"):
                        with mock.patch("builtins.open", return_value=terminal) as opened:
                            with mock.patch.object(setup.subprocess, "run",
                                                   side_effect=results) as run:
                                setup.restart_if_safe(args, changed, action=action)
                                self.assertEqual(run.call_count, 4)
                                self.assertEqual(run.call_args_list[0].args[0],
                                                 ["sudo", "-n", "-v"])
                                self.assertEqual(run.call_args_list[1].args[0],
                                                 ["sudo", "-v"])
                                self.assertIs(run.call_args_list[1].kwargs["stdin"], terminal)
                                self.assertEqual(run.call_args_list[2].args[0],
                                                 ["sudo", "-n", "systemctl",
                                                  "restart", "klipper"])
                                self.assertEqual(run.call_args_list[3].args[0],
                                                 ["sudo", "-n", "systemctl",
                                                  "restart", "moonraker"])
                                self.assertEqual(idle.call_count, 2)
                                opened.assert_called_once_with("/dev/tty", "r+")

    def test_no_terminal_does_not_consume_piped_stdin_or_restart(self):
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        with mock.patch.object(setup, "idle_from_moonraker",
                               return_value=(True, "standby")):
            with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch.object(setup.subprocess, "run",
                                       return_value=SimpleNamespace(returncode=1)) as run:
                    with mock.patch("builtins.open", side_effect=OSError("no tty")):
                        with self.assertRaisesRegex(RuntimeError,
                                                    "no interactive terminal"):
                            setup.restart_if_safe(args, True)
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(run.call_args.args[0], ["sudo", "-n", "-v"])

    def test_password_authentication_failure_does_not_restart(self):
        import io
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        results = [SimpleNamespace(returncode=1), SimpleNamespace(returncode=1)]
        with mock.patch.object(setup, "idle_from_moonraker",
                               return_value=(True, "standby")):
            with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch("builtins.open", return_value=io.StringIO()):
                    with mock.patch.object(setup.subprocess, "run",
                                           side_effect=results) as run:
                        with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                            setup.restart_if_safe(args, True)
                        self.assertEqual(run.call_count, 2)

    def test_authentication_delay_rechecks_active_print(self):
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        with mock.patch.object(setup, "idle_from_moonraker",
                               side_effect=[(True, "standby"),
                                            (False, "print_stats: printing")]) as idle:
            with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch.object(setup.subprocess, "run",
                                       return_value=SimpleNamespace(returncode=0)) as run:
                    setup.restart_if_safe(args, True)
                    self.assertEqual(idle.call_count, 2)
                    self.assertEqual(run.call_count, 1)  # sudo -n -v only
                    self.assertEqual(run.call_args.args[0], ["sudo", "-n", "-v"])

    def test_service_failure_reports_error_and_skips_second_service(self):
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        with mock.patch.object(setup, "idle_from_moonraker",
                               return_value=(True, "standby")):
            with mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch.object(setup.subprocess, "run",
                                       side_effect=[SimpleNamespace(returncode=0),
                                                    SimpleNamespace(returncode=5)]) as run:
                    with self.assertRaisesRegex(RuntimeError,
                                                "Could not restart klipper"):
                        setup.restart_if_safe(args, True)
                    self.assertEqual(run.call_count, 2)

    def test_no_restart_skips_sudo_for_both_actions(self):
        args = SimpleNamespace(
            no_restart=True, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        for action, changed in (("install", True), ("uninstall", False)):
            with mock.patch.object(setup, "idle_from_moonraker") as idle:
                with mock.patch.object(setup.subprocess, "run") as run:
                    setup.restart_if_safe(args, changed, action=action)
                    idle.assert_not_called()
                    run.assert_not_called()

    def test_clean_install_does_not_restart(self):
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        with mock.patch.object(setup, "idle_from_moonraker") as idle:
            with mock.patch.object(setup.subprocess, "run") as run:
                setup.restart_if_safe(args, False, action="install")
                idle.assert_not_called()
                run.assert_not_called()

    def test_uninstall_never_restarts_when_printing(self):
        args = SimpleNamespace(
            no_restart=False, moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper", moonraker_service="moonraker",
        )
        for status in ("print_stats: printing", "print_stats: paused",
                       "Klipper state: shutdown"):
            with mock.patch.object(setup, "idle_from_moonraker",
                                   return_value=(False, status)):
                with mock.patch.object(setup.subprocess, "run") as run:
                    setup.restart_if_safe(args, False, action="uninstall")
                    run.assert_not_called()

    def test_restarts_skipped_when_state_unknown_or_active(self):
        args = SimpleNamespace(
            no_restart=False,
            moonraker_url="http://127.0.0.1:7125",
            klipper_service="klipper",
            moonraker_service="moonraker",
        )
        with mock.patch.object(setup, "idle_from_moonraker", return_value=(False, "printing")):
            with mock.patch.object(setup.subprocess, "run") as run:
                setup.restart_if_safe(args, True)
                run.assert_not_called()
        with mock.patch.object(setup, "idle_from_moonraker", return_value=(False, "unreachable")):
            with mock.patch.object(setup.subprocess, "run") as run:
                setup.restart_if_safe(args, True)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
