import _history
from bitcoin import bind, _1, _2
import json

def wrap_finish(handle_finish, ec, result_json):
    handle_finish(ec, json.loads(result_json))

def payment_history(service, chain, txpool, membuf, address, finish):
    _history.payment_history(service.internal_ptr, chain.internal_ptr,
                             txpool.internal_ptr, membuf.internal_ptr,
                             address, bind(wrap_finish, finish, _1, _2))

