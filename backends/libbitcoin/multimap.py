class MultiMap:

    def __init__(self):
        self.multi = {}

    def __getitem__(self, key):
        return self.multi[key]

    def __setitem__(self, key, value):
        if key not in self.multi:
            self.multi[key] = []
        self.multi[key].append(value)

    def delete(self, key, value):
        for i, item in enumerate(self.multi[key]):
            if item == value:
                del self.multi[key][i]
                if not self.multi[key]:
                    del self.multi[key]
                return
        raise IndexError

    def __repr__(self):
        return repr(self.multi)

    def __str__(self):
        return str(self.multi)

    def has_key(self, key):
        return key in self.multi


if __name__ == "__main__":
    m = MultiMap()
    m["foo"] = 1
    m["foo"] = 1
    m["bar"] = 2
    print m["foo"]
    m.delete("foo", 1)
    m.delete("bar", 2)
    print m.multi
