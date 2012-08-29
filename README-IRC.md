IRC is used by Electrum server to find 'peers' - other Electrum servers. The
current list can be seen by running:

    ./server.py peers

The following config file options are used by the IRC part of Electrum server:

    [server]
    irc = yes
    host = fqdn.host.name.tld
    ircname = a description

`irc` is used to determine whether the IRC thread will be started or the 
Electrum server will run in private mode. In private mode, 
`./server.py peers` will always return an empty list.

`host` is a fully-qualified domain name (FQDN) of your Electrum server. It is
used both when binding the listener for incoming client connections and as part
of the realname field in IRC (see below).

`ircname` is a short text that will be appended to 'host' when composing the 
IRC realname field.

The `realname` = `host` + ' ' + `ircname`. For example, using the example 
configuration above, `realname` would be `fqdn.host.name.tld a description`.
