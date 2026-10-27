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
        for key in ("type: git_repo", "primary_branch: main", "managed_services: klipper"):
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
