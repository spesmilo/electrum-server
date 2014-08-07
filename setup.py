import version
from setuptools import setup

setup(
    name="electrum-server",
    version=version.VERSION,
    scripts=['electrum_server.py'],
    py_modules=[
        'utils.__init__',
        'backends.__init__',
        'backends.bitcoind.__init__',
        'backends.bitcoind.storage',
        'backends.bitcoind.deserialize',
        'backends.bitcoind.networks',
        'backends.bitcoind.blockchain_processor',
        'processor',
        'version',
        'backends.irc.__init__',
        'transports.__init__',
        'transports.poller',
        'transports.stratum_tcp',
        'transports.stratum_http',
    ],
    description="Lightweight Bitcoin Wallet",
    author="Thomas Voegtlin",
    author_email="thomasv1@gmx.de",
    license="GNU GPLv3",
    url="https://electrum.org",
    long_description="""Lightweight Bitcoin Wallet"""
)


