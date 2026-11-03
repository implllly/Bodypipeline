from pipeline.client.native_g1.pico_button_state import AbxyDecoder


def test_complete_combo_is_exclusive_and_requires_full_release():
    decoder = AbxyDecoder()
    assert decoder.decode((True, True, True, True)).start

    # Releasing Y while A+B+X remain held must not become A+X.
    partial = decoder.decode((True, True, True, False))
    assert not partial.ax and not partial.by and not partial.start
    assert not decoder.decode((True, False, True, False)).ax

    decoder.decode((False, False, False, False))
    assert decoder.decode((True, False, True, False)).ax


def test_input_read_failure_never_counts_as_release():
    decoder = AbxyDecoder()
    first = decoder.decode((False, True, False, True))
    assert first.by

    for _ in range(3):
        held = decoder.decode(None)
        assert not held.ax and not held.by and not held.start

    decoder.decode((False, False, False, False))
    assert not decoder.decode((False, False, False, False)).by
    assert decoder.decode((False, True, False, True)).by


def test_each_combo_fires_once_until_every_face_button_is_released():
    decoder = AbxyDecoder()
    assert decoder.decode((True, True, True, True)).start
    for _ in range(4):
        assert not decoder.decode((True, True, True, True)).start
    decoder.decode((False, False, False, False))
    assert decoder.decode((True, True, True, True)).start
