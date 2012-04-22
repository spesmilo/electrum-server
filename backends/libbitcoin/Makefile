CC = g++ -fPIC -Wall -ansi `pkg-config --cflags libbitcoin` -I/usr/include/python2.7 

membuf:
	$(CC) -c membuf.cpp -o membuf.o
	$(CC) -shared -Wl,-soname,membuf.so membuf.o -lpython2.7 -lboost_python `pkg-config --libs libbitcoin` -lboost_thread -o membuf.so

default:
	$(CC) -c history.cpp -o history.o
	$(CC) -shared -Wl,-soname,_history.so history.o -lpython2.7 -lboost_python `pkg-config --libs libbitcoin` -lboost_thread -o history1/_history.so

