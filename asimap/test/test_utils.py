"""
Test our util functions
"""
# System imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..exceptions import Bad
from ..utils import get_uidvv_uid, sequence_set_to_list


####################################################################
#
def test_uidvv_uid(faker):
    uid_vals = [
        (faker.pyint(max_value=999999999), faker.pyint(max_value=999999999))
        for _ in range(10)
    ]
    uids = [f"{x:010d}.{y:010d}" for x, y in uid_vals]
    for x, y in zip(uid_vals, uids):
        assert get_uidvv_uid(y) == x


####################################################################
#
def test_sequence_set_to_list(faker):
    # fmt: off
    valid_sequence_sets = [
        (
            ((319128, 319164),(319169, 319186),(319192, 319210),
             (319212, 319252),(319254, 319256),319258,319261,(319263, 319288),
             (319293, 319389),(319392, 319413),(319415, 319418),
             (319420, 319438),(319440, 319445),(319447, 319455),
             (319457, 319459),(319462, 319487),(319491, 319509),
             (319511, 319514),(319517, 319529),(319531, 319532),
             (319535, 319551),(319553, 319558),(319562, 319595),
             (319598, 319612),(319614, 319617),(319621, 319672),
             (319674, 319681),(319685, 319696)),
            319696
        ),
        (
            ((5637, 5648),(5797, 5800),(5810, 5820),(5823, 6507),(6509, 6623),
             6625),
            6625
        ),
        (
            ((152, 165),(168, 171),(177, 180),192,195,197,199,(205, 224),
             (226, 227),(229, 231),(233, 234),(236, 244),(246, 248),260,268,275,
             (278, 279),281,290,303,308,(316, 320),325,(330, 334),"*"),
            336
        ),
        (
            ((3, 6),(10, 15),(17, 18),20,(22, 27),(30, 33),(35, 47),(49, 59),66,
             (68, 69),72,75,77,(79, 80),85,(87, 90),92,(95, 96),(98, 101),
             (104, 107),110,(113, 120),(122, 123),(126, 132),(134, 145),151,
             (153, 158),(160, 167),(169, 172),(174, 175),177,(179, 181),186,
             (189, 190),(194, 197),200,202,(204, 206),(209, 211),(213, 234),
             237,239,242,(252, 253),260,266,(271, "*")),
            272
        ),
        (
            (1100,(1104, 1113),(1115, 1120),(1122, 1129),(1131, 1146),
             (1148, 1159),(1163, 1167),(1169, 1173),1176,(1178, 1181),
             (1183, 1189),(1191, 1216),(1218, 1230),1232,(1234, 1236),1238,1240,
             1242,(1244, 1246),(1248, 1249),1251,1260,1262,1268,1272,
             (1274, 1282),1284,1288,(1291, 1293),1295,(1298, 1300),1305,1307,
             (1309, 1310)),
            1310
        ),
    ]
    # fmt: on
    for seq_set, seq_max in valid_sequence_sets:
        coalesced = sequence_set_to_list(seq_set, seq_max)
        # Every message id in `coalesced` has to be an int that is either in
        # the seq_set as an int, or is inclusively between one of the tuples.
        #
        for elt in seq_set:
            if isinstance(elt, int):
                assert elt in coalesced
            if isinstance(elt, tuple):
                if elt[1] == "*":
                    elt = (elt[0], seq_max)
                for i in range(elt[0], elt[1] + 1):
                    assert i in coalesced
            if elt == "*":
                assert seq_max in coalesced

    # Bad sequences:
    #

    # '*' in sequence set when max_seq is 0. Only valid if this is a uid
    # command
    #
    with pytest.raises(Bad):
        _ = sequence_set_to_list(("*",), 0)
    coalesced = sequence_set_to_list(("*",), 0, uid_cmd=True)
    assert coalesced == [0]

    # Number in sequence set greater than max_seq
    #
    with pytest.raises(Bad):
        _ = sequence_set_to_list(((1, 10), 20), 10)

    # Inside tuple in sequence set we exceed the seq_max.
    #
    bad_seq_sets = (
        (((1, 10),), 5),
        (((10, 1),), 5),
        (((10, "*"),), 0),
    )
    for seq_set, seq_max in bad_seq_sets:
        with pytest.raises(Bad):
            _ = sequence_set_to_list(seq_set, seq_max)

    # But those bad sequence sets are valid for uid commands
    #
    expected = [
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    ]
    for (seq_set, seq_max), exp in zip(bad_seq_sets, expected):
        coalesced = sequence_set_to_list(seq_set, seq_max, uid_cmd=True)
        assert coalesced == exp
