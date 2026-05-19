app_name = "s3_store"
app_title = "S3 Store"
app_publisher = "Centura AG"
app_description = "S3-backed file storage with native bench backup/restore integration"
app_email = "marc.ramser@centura.ch"
app_license = "mit"

write_file = "s3_store.s3_store.file_handler.write_file"
delete_file_data_content = "s3_store.s3_store.file_handler.delete_file_data_content"

after_migrate = ["s3_store.s3_store.migration.after_migrate_push_local"]

commands = ["s3_store.s3_store.commands.s3_store_commands"]
