from setuptools import setup

setup(
    name="electrum-server",
    version="0.9",
    scripts=['electrum_server.py'],
    py_modules=[
        'src.utils',
        'src.storage',
        'src.deserialize',
        'src.networks',
        'src.blockchain_processor',
        'src.processor',
        'src.version',
        'src.irc',
        'src.poller',
        'src.stratum_tcp',
        'src.stratum_http',
    ],
    description="Lightweight Bitcoin Wallet",
    author="Thomas Voegtlin",
    author_email="thomasv1@gmx.de",
    license="GNU GPLv3",
    url="https://electrum.org",
    long_description="""Lightweight Bitcoin Wallet"""
)


