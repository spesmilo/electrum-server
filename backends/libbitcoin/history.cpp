#include <system_error>

#include <bitcoin/bitcoin.hpp>
using namespace libbitcoin;

#include <boost/python.hpp>
namespace python = boost::python;

#include "/home/genjix/python-bitcoin/src/primitive.h"

typedef std::function<void (const std::error_code&)> finish_handler;

namespace ph = std::placeholders;

class query_history
  : public std::enable_shared_from_this<query_history>
{
public:
    query_history(async_service& service,
        blockchain_ptr chain, transaction_pool_ptr txpool)
      : strand_(service.get_service()), chain_(chain),
        txpool_(txpool), stopped_(false)
    {
    }

    void start(const std::string& address, finish_handler handle_finish)
    {
        address_.set_encoded(address);
        handle_finish_ = handle_finish;
        chain_->fetch_outputs(address_,
            strand_.wrap(std::bind(&query_history::start_loading,
                shared_from_this(), ph::_1, ph::_2)));
    }

private:
    // Not thread-safe
    void stop()
    {
        BITCOIN_ASSERT(stopped_ == false);
        stopped_ = true;
    }

    void start_loading(const std::error_code& ec,
        const message::output_point_list& outpoints)
    {
        handle_finish_(ec);
    }

    io_service::strand strand_;

    blockchain_ptr chain_;
    transaction_pool_ptr txpool_;
    bool stopped_;

    payment_address address_;
    finish_handler handle_finish_;
};

typedef std::shared_ptr<query_history> query_history_ptr;

void keep_query_alive_proxy(const std::error_code& ec,
    python::object handle_finish, query_history_ptr history)
{
    pyfunction<const std::error_code&> f(handle_finish);
    f(ec);
}

void payment_history(async_service_ptr service, blockchain_ptr chain,
    transaction_pool_ptr txpool, const std::string& address,
    python::object handle_finish)
{
    query_history_ptr history =
        std::make_shared<query_history>(*service, chain, txpool);
    history->start(address,
        std::bind(keep_query_alive_proxy, ph::_1,
            handle_finish, history));
}

BOOST_PYTHON_MODULE(_history)
{
    using namespace boost::python;
    def("payment_history", payment_history);
}

