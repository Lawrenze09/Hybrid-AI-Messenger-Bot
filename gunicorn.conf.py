def post_fork(server, worker):
    """Start the webhook worker thread AFTER Gunicorn forks the worker process."""
    from messenger_bot_test import _startup
    _startup()
