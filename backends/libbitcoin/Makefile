CC = g++ -fPIC -Wall -ansi `pkg-config --cflags libbitcoin` -I/usr/include/python2.7 

trace_tx:
	$(CC) -c trace_tx.cpp -o trace_tx.o
	$(CC) -shared -Wl,-soname,trace_tx.so trace_tx.o -lpython2.7 -lboost_python `pkg-config --libs libbitcoin` -lboost_thread -o trace_tx.so

membuf:
	$(CC) -c membuf.cpp -o membuf.o
	$(CC) -shared -Wl,-soname,membuf.so membuf.o -lpython2.7 -lboost_python `pkg-config --libs libbitcoin` -lboost_thread -o membuf.so

default:
	$(CC) -c history.cpp -o history.o
	$(CC) -shared -Wl,-soname,_history.so history.o -lpython2.7 -lboost_python `pkg-config --libs libbitcoin` -lboost_thread -o history1/_history.so

