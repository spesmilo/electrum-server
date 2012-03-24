import sys
import stratum

if __name__ == "__main__":
    backend = __import__("modules." + sys.argv[1], fromlist=["run"])
    stratum_frontend = stratum.Stratum()
    backend.run(stratum_frontend)

