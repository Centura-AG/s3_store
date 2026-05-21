frappe.ui.form.on('S3 Store Settings', {
    refresh(frm) {
        frm.add_custom_button(__('Migrate Local Files to S3'), () => {
            frappe
                .call({ method: 's3_store.s3_store.api.start_migration' })
                .then(({ message }) => {
                    frappe.show_alert({
                        message: __('Migration queued'),
                        indicator: 'green'
                    });
                    frappe.set_route('Form', 'S3 Migration Log', message.log);
                });
        });
    }
});
