#include <bitcoin/bitcoin.hpp>
using namespace libbitcoin;

#include <boost/python.hpp>
namespace python = boost::python;

#include "/home/genjix/python-bitcoin/src/primitive.h"

namespace ph = std::placeholders;

class tracer
  : public std::enable_shared_from_this<tracer>,
    private coroutine
{
public:
    typedef std::vector<std::string> string_list;

    typedef std::function<
        void (const std::error_code&, const string_list&)> finish_handler;

    tracer(async_service& service, blockchain_ptr chain)
      : strand_(service.get_service()), chain_(chain)
    {
    }

    void start(const message::transaction& tx, finish_handler handle_finish)
    {
        auto this_ptr = shared_from_this();
        strand_.post(
            [&, this_ptr, tx, handle_finish]()
            {
                tx_ = std::move(tx);
                handle_finish_ = std::move(handle_finish);
                this_ptr->run(std::error_code());
            });
    }

    void run(const std::error_code& ec);

private:
    io_service::strand strand_;
    blockchain_ptr chain_;

    message::transaction tx_;
    finish_handler handle_finish_;

    size_t count_;
    bool stopped_;
    std::vector<script> output_scripts_;
};

typedef std::shared_ptr<tracer> tracer_ptr;

void tracer::run(const std::error_code& ec)
{
    auto this_ptr = shared_from_this();
    reenter(this)
    {
        yield
        {
            count_ = 0;
            stopped_ = false;
            output_scripts_.resize(tx_.inputs.size());
            for (size_t input_idx = 0;
                input_idx < tx_.inputs.size(); ++input_idx)
            {
                const auto& tx_input = tx_.inputs[input_idx];
                auto prevout_idx = tx_input.previous_output.index;
                chain_->fetch_transaction(
                    tx_input.previous_output.hash,
                    strand_.wrap([&, this_ptr, input_idx, prevout_idx]
                        (const std::error_code& ec,
                            const message::transaction& prev_tx)
                        {
                            if (stopped_)
                                return;
                            if (ec)
                            {
                                this_ptr->run(ec);
                                return;
                            }
                            BITCOIN_ASSERT(
                                prevout_idx < prev_tx.outputs.size());
                            BITCOIN_ASSERT(
                                input_idx < output_scripts_.size());
                            output_scripts_[input_idx] =
                                prev_tx.outputs[prevout_idx].output_script;
                            ++count_;
                            if (count_ == tx_.inputs.size())
                                this_ptr->run(ec);
                        }));
            }
        }
        if (ec)
        {
            stopped_ = true;
            handle_finish_(ec, string_list());
            return;
        }
        string_list result;
        for (const auto& outscript: output_scripts_)
        {
            payment_address addr;
            if (!extract(addr, outscript))
            {
                stopped_ = true;
                handle_finish_(ec, string_list());
                return;
            }
            result.push_back(addr.encoded());
        }
        handle_finish_(std::error_code(), result);
    }
}

void keep_trace_alive_proxy(const std::error_code& ec,
    const tracer::string_list& input_addrs,
    python::object handle_finish, tracer_ptr trace)
{
    python::list result;
    for (const auto& addr: input_addrs)
        result.append(addr);
    pyfunction<const std::error_code&, python::list> f(handle_finish);
    f(ec, result);
}

void trace_tx(async_service_ptr service, blockchain_ptr chain,
    const message::transaction& tx, python::object handle_finish)
{
    auto trace = std::make_shared<tracer>(*service, chain);
    trace->start(tx,
        std::bind(keep_trace_alive_proxy, ph::_1, ph::_2,
            handle_finish, trace));
}

BOOST_PYTHON_MODULE(trace_tx)
{
    using namespace boost::python;
    def("trace_tx", trace_tx);
}

