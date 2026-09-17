from types import SimpleNamespace
from unittest.mock import patch

import frappe
from click.testing import CliRunner
from frappe.tests.utils import FrappeTestCase

from s3_store.s3_store import commands


class TestPushLocalCommand(FrappeTestCase):
    def _invoke(self, args, result=None, isatty=False, stdin=None):
        result = result or {"pushed": 0, "failed": 0, "errors": []}
        with (
            patch.object(frappe, "init"),
            patch.object(frappe, "connect"),
            patch.object(frappe, "destroy"),
            patch.object(
                commands,
                "sys",
                SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: isatty)),
            ),
            patch(
                "s3_store.s3_store.migration.push_local_files_to_s3",
                return_value=result,
            ) as push,
        ):
            out = CliRunner().invoke(
                commands.s3_store_push_local,
                args,
                obj=frappe._dict(sites=["s3_store.localhost"], profile=False),
                input=stdin,
            )
        return out, push

    def test_reports_the_summary(self):
        out, push = self._invoke(
            ["--keep-local"], {"pushed": 3, "failed": 0, "errors": []}
        )
        self.assertEqual(out.exit_code, 0, out.output)
        push.assert_called_once_with(delete_local=False)
        self.assertIn("pushed 3 files; failed 0", out.output)

    def test_lists_errors_and_caps_the_list(self):
        errors = [f"F{i}: boom" for i in range(25)]
        out, _ = self._invoke(
            ["--keep-local"], {"pushed": 0, "failed": 25, "errors": errors}
        )
        self.assertEqual(out.exit_code, 0, out.output)
        self.assertIn("F0: boom", out.output)
        self.assertIn("... and 5 more", out.output)
        self.assertNotIn("F24: boom", out.output)

    def test_defaults_delete_local_to_the_site_setting(self):
        settings = frappe.get_single("S3 Store Settings")
        settings.delete_local_after_push = 1
        settings.save(ignore_permissions=True)
        frappe.clear_cache(doctype="S3 Store Settings")
        out, push = self._invoke([])
        self.assertEqual(out.exit_code, 0, out.output)
        push.assert_called_once_with(delete_local=True)

    def test_asks_before_deleting_local_files_on_a_terminal(self):
        out, push = self._invoke(["--delete-local"], isatty=True, stdin="n\n")
        self.assertEqual(out.exit_code, 1)
        push.assert_not_called()
