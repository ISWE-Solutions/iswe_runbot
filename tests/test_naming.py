from previewer.naming import db_name, host_label, preview_host, slugify


def test_slugify():
    assert slugify("Feature/Tax_Fix--2") == "feature-tax-fix-2"
    assert slugify("--v18.0--") == "v18-0"


def test_host_label_short():
    assert host_label("payroll", "v18") == "payroll-v18"
    assert preview_host("payroll", "v18", "preview.localhost") == "payroll-v18.preview.localhost"


def test_host_label_truncated_with_hash():
    label = host_label("payroll", "x" * 80)
    assert len(label) == 63
    assert label[-7] == "-" and label[:-7].startswith("payroll-xxx")
    assert host_label("payroll", "x" * 80) != host_label("payroll", "x" * 81)


def test_db_name():
    assert db_name("payroll", "v18", "6f2e1b0abc", 12) == "payroll_v18_6f2e1b0_b12"
    long = db_name("p" * 40, "b" * 40, "abcdef1234", 7)
    assert len(long) <= 63 and long.endswith("_b7")
