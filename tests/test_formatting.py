from app.telegram.formatting import to_telegram_html


def test_bold():
    assert to_telegram_html("Toto je **dôležité** slovo.") == "Toto je <b>dôležité</b> slovo."


def test_italic():
    assert to_telegram_html("Toto je *zvýraznené* slovo.") == "Toto je <i>zvýraznené</i> slovo."


def test_heading_becomes_bold():
    assert to_telegram_html("### Nadpis\n\nText.") == "<b>Nadpis</b>\n\nText."


def test_bullet_list():
    result = to_telegram_html("* prvý\n* druhý")
    assert result == "• prvý\n• druhý"


def test_horizontal_rule_stripped():
    result = to_telegram_html("Pred.\n\n---\n\nPo.")
    assert "---" not in result
    assert "Pred." in result
    assert "Po." in result


def test_blockquote():
    result = to_telegram_html("> riadok jedna\n> riadok dva")
    assert result == "<blockquote>riadok jedna\nriadok dva</blockquote>"


def test_inline_code():
    assert to_telegram_html("Použi `typ='weight'`.") == "Použi <code>typ='weight'</code>."


def test_link():
    result = to_telegram_html("Pozri [tento odkaz](https://example.com).")
    assert result == 'Pozri <a href="https://example.com">tento odkaz</a>.'


def test_html_special_chars_escaped():
    result = to_telegram_html("Hodnota 5 < 10 & 20 > 15.")
    assert result == "Hodnota 5 &lt; 10 &amp; 20 &gt; 15."


def test_bullet_not_confused_with_italic():
    # A leading "* item" bullet must not get eaten by the italic regex as if it
    # were pairing up with some later stray asterisk in the text.
    result = to_telegram_html("* prvá položka\n* druhá s *kurzívou* v nej")
    assert result == "• prvá položka\n• druhá s <i>kurzívou</i> v nej"
