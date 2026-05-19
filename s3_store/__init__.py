__version__ = "0.1.0"


def _install_backup_patch():
    from s3_store.s3_store.backup import patch_backup_generator

    patch_backup_generator()


_install_backup_patch()
