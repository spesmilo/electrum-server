import stratum

class LibbitcoinProcessor(stratum.Processor):

    def process(self, session):
        request = session.pop_request()
        print "New request (lib)", request
        # Execute and when ready, you call
        # session.push_response(response)

if __name__ == "__main__":
    processor = LibbitcoinProcessor()
    app = stratum.Stratum()
    app.start(processor)

