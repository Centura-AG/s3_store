__version__ = "1.1.0"


def _install_backup_patch():
    from s3_store.s3_store.backup import patch_backup_generator

    patch_backup_generator()


def _install_restore_patch():
    from s3_store.s3_store.backup import patch_restore_push

    patch_restore_push()


_install_backup_patch()
_install_restore_patch()
