__author__ = 'erasmospunk'

import unittest
from utils import hash_160_to_address, bc_address_to_hash_160


class UtilTest(unittest.TestCase):

    def test_hash_160_to_address(self):
        self.assertEqual(hash_160_to_address(None), None)
        self.assertEqual(hash_160_to_address('04e9fca1'.decode('hex')), None)
        self.assertEqual(hash_160_to_address('04e9fca1f96e021dfaf35bbea267ec2c60787c1b1337'.decode('hex')), None)
        self.assertEqual(hash_160_to_address('1ad3b0b711f211655a01142fbb8fecabe8e30b93'.decode('hex')),
                         '13SrAVFPVW1txSj34B8Bd6hnDbyPsVGa92')


    def test_bc_address_to_hash_160(self):
        self.assertEqual(bc_address_to_hash_160(None), None)
        self.assertEqual(bc_address_to_hash_160(''), None)
        self.assertEqual(bc_address_to_hash_160('13SrAVFPVW1txSj34B8Bd6hnDbyPsVGa921337'), None)
        self.assertEqual(bc_address_to_hash_160('13SrAVFPVW1txSj34B8Bd6hnDbyPsVGa92').encode('hex'),
                                                '1ad3b0b711f211655a01142fbb8fecabe8e30b93')



if __name__ == '__main__':
    unittest.main()

