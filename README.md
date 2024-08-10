# rfctf-sdr-tools

## GNU Radio ZMQ Receivers

These ZMQ receivers can be opened in GNUradio and used to modify a signal into a visible and audible format. The virtual machines used for the virtual wifi *can* be used, but performance will be poor.  If possible, it is recommended to use Gnuradio on your local computer for better performance.

https://zeromq.org/

The server is sdr.rfhackers.com ports 6550-6570. By modifying the 'port' variable, you can access each of the different challenges.

**Note:** If you're on a low bandwidth connection, it's recommended to save the IQ stream to a file first, then decode the information from the file. There is a file sync in 3.8 and a wav file sync in 3.10. They are disabled by default, right click and enable to save to a file and use in your favorite program.
