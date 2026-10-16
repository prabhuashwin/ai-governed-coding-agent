"""Work-item sources: where tasks come from and where status goes back to.

The local queue is the default. `ServiceNowSource` is the adapter sketched in the proposal:
poll a scoped table for approved work items, submit them to the queue, and write status,
report and audit reference back to the record.
"""
