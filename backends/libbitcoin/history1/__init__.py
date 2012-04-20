import _history
from bitcoin import bind, _1

def wrap_finish(handle_finish, ec):
    handle_finish(ec)

def payment_history(service, chain, txpool, address, finish):
    _history.payment_history(service.internal_ptr, chain.internal_ptr,
                             txpool.internal_ptr, address,
                             bind(wrap_finish, finish, _1))

