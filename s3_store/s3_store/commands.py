"""bench-level click commands. Wired via hooks.py `commands = [...]`."""

import sys

import click
import frappe
from frappe.commands import get_site, pass_context


@click.command("s3-store-push-local")
@click.option(
    "--delete-local/--keep-local",
    default=None,
    help="Delete local files after upload. Defaults to the site's S3 Store Settings value.",
)
@pass_context
def s3_store_push_local(context, delete_local):
    """Upload any local files back to S3 (run after `bench restore`)."""
    from s3_store.s3_store.migration import push_local_files_to_s3

    site = get_site(context)
    frappe.init(site=site)
    frappe.connect()
    try:
        if delete_local is None:
            settings = frappe.get_cached_doc("S3 Store Settings", "S3 Store Settings")
            delete_local = bool(settings.delete_local_after_push)

        if delete_local and sys.stdin.isatty():
            click.confirm("Delete local files after uploading to S3?", abort=True)

        result = push_local_files_to_s3(delete_local=delete_local)
        click.echo(
            f"s3_store: pushed {result['pushed']} files; failed {result['failed']}"
        )
        if result["errors"]:
            click.echo("Errors:")
            for err in result["errors"][:20]:
                click.echo(f"  - {err}")
            if len(result["errors"]) > 20:
                click.echo(f"  ... and {len(result['errors']) - 20} more")
    finally:
        frappe.destroy()


commands = [s3_store_push_local]
