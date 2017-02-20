import random


def magic_number(num):
    return num * random.randint(1, 20)


class FilterModule(object):

    def filters(self):
        return {
            'magic_number': magic_number,
        }
