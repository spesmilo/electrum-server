#include <bitcoin/bitcoin.hpp>
using namespace libbitcoin;

#include "/home/genjix/python-bitcoin/src/primitive.h"

namespace ph = std::placeholders;

int cmp(const bc::message::output_point& a,
    const bc::message::output_point& b)
{
    if (a.index < b.index)
        return -1;
    else if (a.index > b.index)
        return 1;
    // a.index == b.index
    if (a.hash < b.hash)
        return -1;
    else if (a.hash > b.hash)
        return 1;
    return 0;
}

struct outpoint_less_cmp
{
    bool operator()(const bc::message::output_point& a,
        const bc::message::output_point& b)
    {
        return cmp(a, b) == -1;
    }
};

struct address_less_cmp
{
    bool operator()(const payment_address& a, const payment_address& b)
    {
        if (a.hash() < b.hash())
            return true;
        else if (a.hash() > b.hash())
            return false;
        if (a.version() < b.version())
            return true;
        return false;
    }
};

class memory_buffer
  : public std::enable_shared_from_this<memory_buffer>
{
public:
    struct check_item
    {
        hash_digest tx_hash;
        size_t index;
        bool is_input;
        uint64_t timestamp;
    };
    typedef std::vector<check_item> check_result;

    typedef std::function<
        void (const std::error_code&)> receive_handler;
    typedef std::function<
        void (const std::error_code&, const check_result&)> check_handler;

    memory_buffer(async_service_ptr service, blockchain_ptr chain,
        transaction_pool_ptr txpool)
      : strand_(service->get_service()), chain_(chain), txpool_(txpool)
    {
    }

    void set_handles(python::object handle_tx_stored,
        python::object handle_tx_confirmed)
    {
        auto this_ptr = shared_from_this();
        strand_.post(
            [&, this_ptr, handle_tx_stored, handle_tx_confirmed]()
            {
                handle_tx_stored_ = handle_tx_stored;
                handle_tx_confirmed_ = handle_tx_confirmed;
            });
    }

    void receive(const message::transaction& tx,
        python::object handle_receive)
    {
        txpool_->store(tx,
            strand_.wrap(std::bind(&memory_buffer::confirmed,
                shared_from_this(), ph::_1, tx)),
            strand_.wrap(std::bind(&memory_buffer::stored,
                shared_from_this(), ph::_1, tx, handle_receive)));
    }

    void check(const message::output_point_list& output_points,
        const payment_address& address, check_handler handle_check)
    {
        auto this_ptr = shared_from_this();
        strand_.post(
            [&, this_ptr, output_points, address, handle_check]()
            {
                check_result result;
                for (auto& outpoint: output_points)
                {
                    auto it = lookup_input_.find(outpoint);
                    if (it != lookup_input_.end())
                    {
                        check_item item;
                        item.tx_hash = it->second.hash;
                        item.index = it->second.index;
                        item.is_input = true;
                        item.timestamp = timestamps_[item.tx_hash];
                        result.push_back(item);
                    }
                }
                auto range = lookup_address_.equal_range(address);
                for (auto it = range.first; it != range.second; ++it)
                {
                    check_item item;
                    item.tx_hash = it->second.hash;
                    item.index = it->second.index;
                    item.is_input = false;
                    item.timestamp = timestamps_[item.tx_hash];
                    result.push_back(item);
                }
                handle_check(std::error_code(), result);
            });
    }

private:
    void stored(const std::error_code& ec, const message::transaction& tx,
        python::object handle_receive)
    {
        if (ec)
        {
            pyfunction<const std::error_code&> f(handle_receive);
            f(ec);
            return;
        }
        const hash_digest& tx_hash = hash_transaction(tx);
        for (uint32_t input_index = 0;
            input_index < tx.inputs.size(); ++input_index)
        {
            const auto& prevout = tx.inputs[input_index].previous_output;
            lookup_input_[prevout] =
                message::input_point{tx_hash, input_index};
        }
        for (uint32_t output_index = 0;
            output_index < tx.outputs.size(); ++output_index)
        {
            payment_address address;
            if (extract(address, tx.outputs[output_index].output_script))
            {
                lookup_address_.insert(
                    std::make_pair(address,
                        message::output_point{tx_hash, output_index}));
            }
        }
        timestamps_[tx_hash] = time(nullptr);
        pyfunction<const std::error_code&> f(handle_receive);
        f(std::error_code());
        // tx stored
        if (!handle_tx_stored_.is_none())
        {
            pyfunction<const message::transaction&> g(handle_tx_stored_);
            g(tx);
        }
    }

    void confirmed(const std::error_code& ec, const message::transaction& tx)
    {
        const hash_digest& tx_hash = hash_transaction(tx);
        if (ec)
        {
            std::cerr << "Problem confirming transaction "
                << pretty_hex(tx_hash) << " : " << ec.message() << std::endl;
            return;
        }
        std::cout << "Confirmed " << pretty_hex(tx_hash) << std::endl;
        for (uint32_t input_index = 0;
            input_index < tx.inputs.size(); ++input_index)
        {
            const auto& prevout = tx.inputs[input_index].previous_output;
            auto it = lookup_input_.find(prevout);
            BITCOIN_ASSERT(it != lookup_input_.end());
            BITCOIN_ASSERT((it->second ==
                message::input_point{tx_hash, input_index}));
            lookup_input_.erase(it);
        }
        for (uint32_t output_index = 0;
            output_index < tx.outputs.size(); ++output_index)
        {
            message::output_point outpoint{tx_hash, output_index};
            payment_address address;
            if (extract(address, tx.outputs[output_index].output_script))
            {
                auto range = lookup_address_.equal_range(address);
                auto it = range.first;
                for (; it != range.second; ++it)
                {
                    if (it->second == outpoint)
                    {
                        lookup_address_.erase(it);
                        break;
                    }
                }
                BITCOIN_ASSERT(it != range.second);
            }
        }
        auto time_it = timestamps_.find(tx_hash);
        BITCOIN_ASSERT(time_it != timestamps_.end());
        timestamps_.erase(time_it);
        // tx_confirmed
        if (!handle_tx_stored_.is_none())
        {
            pyfunction<const message::transaction&> f(handle_tx_confirmed_);
            f(tx);
        }
    }

    io_service::strand strand_;
    blockchain_ptr chain_;
    transaction_pool_ptr txpool_;

    std::map<message::output_point,
        message::input_point, outpoint_less_cmp> lookup_input_;
    std::multimap<payment_address,
        message::output_point, address_less_cmp> lookup_address_;
    std::map<hash_digest, uint64_t> timestamps_;

    python::object handle_tx_stored_, handle_tx_confirmed_;
};

typedef std::shared_ptr<memory_buffer> memory_buffer_ptr;

