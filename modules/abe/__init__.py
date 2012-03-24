import stratum

class AbeProcessor(stratum.Processor):

    def __init__(self):
        stratum.Processor.__init__(self)

    def stop(self):
        pass

    def process(self, session):
        request = session.pop_request()
        print request
        # session.push_response(response)

def run(stratum):
    processor = AbeProcessor()
    stratum.start(processor)

