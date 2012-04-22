#include <boost/python.hpp>
namespace python = boost::python;

#include "memory_buffer.hpp"

struct memory_buffer_wrapper
{
    memory_buffer_wrapper(async_service_ptr service, blockchain_ptr chain,
        transaction_pool_ptr txpool)
    {
        membuf = std::make_shared<memory_buffer>(service, chain, txpool);
    }

    void set_handles(python::object handle_tx_stored,
        python::object handle_tx_confirmed)
    {
        membuf->set_handles(handle_tx_stored, handle_tx_confirmed);
    }

    void receive(const message::transaction& tx,
        python::object handle_receive)
    {
        membuf->receive(tx, handle_receive);
    }

    memory_buffer_ptr membuf;
};

BOOST_PYTHON_MODULE(membuf)
{
    using namespace boost::python;
    class_<memory_buffer_wrapper>("memory_buffer", init<
            async_service_ptr, blockchain_ptr, transaction_pool_ptr>())
        .def("receive", &memory_buffer_wrapper::receive)
        .def("set_handles", &memory_buffer_wrapper::set_handles)
        .def_readonly("internal_ptr", &memory_buffer_wrapper::membuf)
    ;
    class_<memory_buffer_ptr>("internal_memory_buffer", no_init);
}

