from webapp.backend.app import database


def test_connection_inherited_from_another_pid_is_replaced(monkeypatch):
    database.engine.dispose()
    monkeypatch.setattr(database, "_current_pid", lambda: 1001)
    with database.engine.connect() as connection:
        original = connection.connection.driver_connection
        assert connection.connection._connection_record.info["pid"] == 1001

    monkeypatch.setattr(database, "_current_pid", lambda: 1002)
    with database.engine.connect() as connection:
        replacement = connection.connection.driver_connection
        assert connection.connection._connection_record.info["pid"] == 1002

    assert replacement is not original
