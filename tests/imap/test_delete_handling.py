from datetime import datetime, timedelta
import pytest
from sqlalchemy import desc, inspect
from sqlalchemy.orm.exc import ObjectDeletedError
from inbox.crispin import GmailFlags
from inbox.mailsync.backends.imap.common import (remove_deleted_uids,
                                                 update_metadata)
from inbox.mailsync.gc import DeleteHandler
from inbox.models import Folder, Transaction
from tests.util.base import add_fake_imapuid, add_fake_message


@pytest.fixture()
def marked_deleted_message(db, message):
    deleted_timestamp = datetime(2015, 2, 22, 22, 22, 22)
    message.deleted_at = deleted_timestamp
    db.session.commit()
    return message


def test_messages_deleted_asynchronously(db, default_account, thread, message,
                                         imapuid, folder):
    msg_uid = imapuid.msg_uid
    update_metadata(default_account.id, folder.id,
                    {msg_uid: GmailFlags((), ('label',))}, db.session)
    assert 'label' in [cat.display_name for cat in message.categories]
    remove_deleted_uids(default_account.id, folder.id, [msg_uid], db.session)
    assert abs((message.deleted_at - datetime.utcnow()).total_seconds()) < 2
    # Check that message categories do get updated synchronously.
    assert 'label' not in [cat.display_name for cat in message.categories]


def test_drafts_deleted_synchronously(db, default_account, thread, message,
                                      imapuid, folder):
    message.is_draft = True
    msg_uid = imapuid.msg_uid
    remove_deleted_uids(default_account.id, folder.id, [msg_uid], db.session)
    db.session.expire_all()
    assert inspect(message).deleted
    assert inspect(thread).deleted


def test_deleting_from_a_message_with_multiple_uids(db, default_account,
                                                    message, thread):
    """Check that deleting a imapuid from a message with
    multiple uids doesn't mark the message for deletion."""
    inbox_folder = Folder.find_or_create(db.session, default_account, 'inbox',
                                         'inbox')
    sent_folder = Folder.find_or_create(db.session, default_account, 'sent',
                                         'sent')

    add_fake_imapuid(db.session, default_account.id, message, sent_folder,
                     1337)
    add_fake_imapuid(db.session, default_account.id, message, inbox_folder,
                     2222)

    assert len(message.imapuids) == 2

    remove_deleted_uids(default_account.id, inbox_folder.id, [2222],
                        db.session)

    assert message.deleted_at is None, \
        "The associated message should not have been marked for deletion."

    assert len(message.imapuids) == 1, \
        "The message should have only one imapuid."


def test_deletion_with_short_ttl(db, default_account, default_namespace,
                                 marked_deleted_message, thread, folder):
    handler = DeleteHandler(account_id=default_account.id,
                            namespace_id=default_namespace.id,
                            uid_accessor=lambda m: m.imapuids,
                            message_ttl=0)
    handler.check(marked_deleted_message.deleted_at + timedelta(seconds=1))
    db.session.expire_all()
    # Check that objects were actually deleted
    with pytest.raises(ObjectDeletedError):
        marked_deleted_message.id
    with pytest.raises(ObjectDeletedError):
        thread.id


def test_non_orphaned_messages_get_unmarked(db, default_account,
                                            default_namespace,
                                            marked_deleted_message, thread,
                                            folder, imapuid):
    handler = DeleteHandler(account_id=default_account.id,
                            namespace_id=default_namespace.id,
                            uid_accessor=lambda m: m.imapuids,
                            message_ttl=0)
    handler.check(marked_deleted_message.deleted_at + timedelta(seconds=1))
    db.session.expire_all()
    # message actually has an imapuid associated, so check that the
    # DeleteHandler unmarked it.
    assert marked_deleted_message.deleted_at is None


def test_threads_only_deleted_when_no_messages_left(db, default_account,
                                                    default_namespace,
                                                    marked_deleted_message,
                                                    thread, folder):
    handler = DeleteHandler(account_id=default_account.id,
                            namespace_id=default_namespace.id,
                            uid_accessor=lambda m: m.imapuids,
                            message_ttl=0)
    # Add another message onto the thread
    add_fake_message(db.session, default_namespace.id, thread)

    handler.check(marked_deleted_message.deleted_at + timedelta(seconds=1))
    db.session.expire_all()
    # Check that the orphaned message was deleted.
    with pytest.raises(ObjectDeletedError):
        marked_deleted_message.id
    # Would raise ObjectDeletedError if thread was deleted.
    thread.id


def test_deletion_deferred_with_longer_ttl(db, default_account,
                                           default_namespace,
                                           marked_deleted_message, thread,
                                           folder):
    handler = DeleteHandler(account_id=default_account.id,
                            namespace_id=default_namespace.id,
                            uid_accessor=lambda m: m.imapuids,
                            message_ttl=5)
    db.session.commit()

    handler.check(marked_deleted_message.deleted_at + timedelta(seconds=1))
    # Would raise ObjectDeletedError if objects were deleted
    marked_deleted_message.id
    thread.id


def test_deletion_creates_revision(db, default_account, default_namespace,
                                   marked_deleted_message, thread, folder):
    message_id = marked_deleted_message.id
    thread_id = thread.id
    handler = DeleteHandler(account_id=default_account.id,
                            namespace_id=default_namespace.id,
                            uid_accessor=lambda m: m.imapuids,
                            message_ttl=0)
    handler.check(marked_deleted_message.deleted_at + timedelta(seconds=1))
    db.session.commit()
    latest_message_transaction = db.session.query(Transaction). \
        filter(Transaction.record_id == message_id,
               Transaction.object_type == 'message',
               Transaction.namespace_id == default_namespace.id). \
        order_by(desc(Transaction.id)).first()
    assert latest_message_transaction.command == 'delete'

    latest_thread_transaction = db.session.query(Transaction). \
        filter(Transaction.record_id == thread_id,
               Transaction.object_type == 'thread',
               Transaction.namespace_id == default_namespace.id). \
        order_by(desc(Transaction.id)).first()
    assert latest_thread_transaction.command == 'delete'
