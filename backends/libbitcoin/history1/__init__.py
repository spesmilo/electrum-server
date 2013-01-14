import json

from bitcoin import bind, _1, _2
import _history


def wrap_finish(handle_finish, ec, result_json):
    try:
        result = json.loads(result_json)
    except ValueError:
        print result_json
        raise
    else:
        handle_finish(ec, result)


def payment_history(service, chain, txpool, membuf, address, finish):
    _history.payment_history(service.internal_ptr, chain.internal_ptr,
                             txpool.internal_ptr, membuf.internal_ptr,
                             str(address), bind(wrap_finish, finish, _1, _2))
