from psi.keys import archived_sale_key, current_sale_key


def test_current_key_format():
    assert current_sale_key("001", "12345", "7") == "C:001:12345:7"


def test_archived_key_normalizes_case_and_whitespace():
    a = archived_sale_key("213", "1a", " 27A ", "bathurst st", "DUBBO", "2830",
                          "1995-06-15", 120000)
    b = archived_sale_key("213", "1A", "27A", "BATHURST ST", "Dubbo", "2830",
                          "1995-06-15", 120000)
    assert a == b


def test_archived_key_none_equals_blank():
    a = archived_sale_key("213", None, "27A", "X ST", "DUBBO", "2830",
                          "1995-06-15", None)
    b = archived_sale_key("213", "", "27A", "X ST", "DUBBO", "2830",
                          "1995-06-15", None)
    assert a == b


def test_archived_key_sensitive_to_each_field():
    base = archived_sale_key("213", "1A", "27A", "X ST", "DUBBO", "2830",
                             "1995-06-15", 120000)
    variants = [
        archived_sale_key("214", "1A", "27A", "X ST", "DUBBO", "2830", "1995-06-15", 120000),
        archived_sale_key("213", "2A", "27A", "X ST", "DUBBO", "2830", "1995-06-15", 120000),
        archived_sale_key("213", "1A", "27A", "X ST", "DUBBO", "2830", "1995-06-16", 120000),
        archived_sale_key("213", "1A", "27A", "X ST", "DUBBO", "2830", "1995-06-15", None),
    ]
    assert base not in variants
