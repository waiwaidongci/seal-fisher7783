from src.services import rewind


def list_shifts(db):
    return rewind.list_shifts(db)


def create_shift(db, data, user):
    return rewind.create_shift(db, data, user)


def get_shift(db, shift_id):
    return rewind.get_shift(db, shift_id)


def amend_shift(db, shift_id, patch, user):
    return rewind.amend_shift(db, shift_id, patch, user)


def restore(db, shift_id, user):
    return rewind.restore(db, shift_id, user)


def get_restoration(db, shift_id, restoration_id):
    return rewind.get_restoration(db, shift_id, restoration_id)


def adopt(db, shift_id, restoration_id, data, user):
    return rewind.adopt(db, shift_id, restoration_id, data, user)
