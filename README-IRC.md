IRC is used by Electrum server to find 'peers' - other Electrum servers. The
current list can be seen by running:

    ./server.py peers

The following config file options are used by the IRC part of Electrum server:

    [server]
    irc = yes
    host = fqdn.host.name.tld
    irc_nick = server nickname

`irc` is used to determine whether the IRC thread will be started or the 
Electrum server will run in private mode. In private mode, 
`./server.py peers` will always return an empty list.

`host` is a fully-qualified domain name (FQDN) of your Electrum server. It is
used both when binding the listener for incoming client connections and as part
of the realname field in IRC (see below).

`irc_nick` is a nick name that will be appended to E_ when 
composing the IRC nickname to identify your server on #electrum.

Please note the IRC name field can only contain 50 chars and will be composed
of `host` + protocol version number + Port numbers for the various protocols.
Please check whether port numbers are cut off at the end   

