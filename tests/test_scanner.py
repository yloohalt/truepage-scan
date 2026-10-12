from io import BytesIO
import numpy as np
import pytest
from PIL import Image
from truepage.scanner import decode, png, proposals, rectify, rotate, validate_corners


def fixture_image():
    rgb = np.full((300, 400, 3), 240, np.uint8)
    rgb[20:80, 20:80] = (240, 0, 0)
    rgb[220:280, 320:380] = (0, 40, 240)
    return rgb


def test_identity_warp_preserves_every_pixel():
    rgb = fixture_image()
    out = rectify(rgb, [[0,0],[399,0],[399,299],[0,299]])
    assert np.array_equal(out, rgb)
    assert np.array_equal(decode(png(out)), rgb)


def test_crop_preserves_correct_page():
    rgb = fixture_image()
    out = rectify(rgb, [[0,0],[199,0],[199,299],[0,299]])
    assert out.shape == (300, 200, 3)
    assert np.array_equal(out, rgb[:, :200])


@pytest.mark.parametrize("corners", [
    [[0,0],[399,299],[399,0],[0,299]],
    [[-1,0],[399,0],[399,299],[0,299]],
    [[0,0],[399,0],[399,float('nan')],[0,299]],
    [[0,0],[2,0],[2,2],[0,2]],
    [[0,0],[0,299],[399,299],[399,0]],
])
def test_invalid_geometry_rejected(corners):
    with pytest.raises(ValueError):
        validate_corners(corners, 400, 300)


def test_exif_orientation_and_rotation():
    im = Image.fromarray(fixture_image())
    exif = im.getexif(); exif[274] = 6
    b = BytesIO(); im.save(b, format="JPEG", exif=exif)
    upright = decode(b.getvalue())
    assert upright.shape == (400, 300, 3)
    assert rotate(upright, 270).shape == (300, 400, 3)
    with pytest.raises(ValueError):
        rotate(upright, 45)


def test_bad_image_rejected():
    with pytest.raises(ValueError):
        decode(b'not an image')
    with pytest.raises(ValueError):
        decode(b'x' * (12 * 1024 * 1024 + 1))


def test_transparent_png_composited_white():
    im = Image.new('RGBA', (100,100), (0,0,0,0))
    b = BytesIO(); im.save(b, format='PNG')
    assert (decode(b.getvalue()) == 255).all()


def test_proposals_always_require_review():
    result = proposals(fixture_image())
    assert result['review_required'] is True
    assert result['width'] == 400
