import os
import pickle
import argparse
from pathlib import Path
from PIL import Image

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import cv2
import numpy as np

from oemer import MODULE_PATH
from oemer import layers
from oemer.inference import inference
from oemer.utils import get_logger
from oemer.dewarp import estimate_coords, dewarp
from oemer.staffline_extraction import extract as staff_extract
from oemer.notehead_extraction import extract as note_extract
from oemer.note_group_extraction import extract as group_extract
from oemer.symbol_extraction import extract as symbol_extract
from oemer.rhythm_extraction import extract as rhythm_extract
from oemer.build_system import MusicXMLBuilder


logger = get_logger(__name__)


def clear_data():
    lls = layers.list_layers()
    for l in lls:
        layers.delete_layer(l)


def generate_pred(img_path):
    logger.info("Extracting staffline and symbols")
    staff_symbols_map, _ = inference(os.path.join(MODULE_PATH, "checkpoints/unet_big"), img_path)
    staff = np.where(staff_symbols_map==1, 1, 0)
    symbols = np.where(staff_symbols_map==2, 1, 0)

    logger.info("Extracting layers of different symbols")
    symbol_thresholds = [0.5, 0.4, 0.4]
    sep, _ = inference(os.path.join(MODULE_PATH, "checkpoints/seg_net"), img_path, manual_th=None)
    stems_rests = np.where(sep==1, 1, 0)
    notehead = np.where(sep==2, 1, 0)
    clefs_keys = np.where(sep==3, 1, 0)
    # stems_rests = sep[..., 0]
    # notehead = sep[..., 1]
    # clefs_keys = sep[..., 2]

    return staff, symbols, stems_rests, notehead, clefs_keys


def polish_symbols(rgb_black_th=300):
    img = layers.get_layer('original_image')
    sym_pred = layers.get_layer('symbols_pred')

    img = Image.fromarray(img).resize((sym_pred.shape[1], sym_pred.shape[0]))
    arr = np.sum(np.array(img), axis=-1)
    arr = np.where(arr < rgb_black_th, 1, 0)  # Filter background
    ker = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3))
    arr = cv2.dilate(cv2.erode(arr.astype(np.uint8), ker), ker)  # Filter staff lines
    mix = np.where(sym_pred+arr>1, 1, 0)
    return mix


def register_notehead_bbox(bboxes):
    symbols = layers.get_layer('symbols_pred')
    layer = layers.get_layer('bboxes')
    for (x1, y1, x2, y2) in bboxes:
        yi, xi = np.where(symbols[y1:y2, x1:x2]>0)
        yi += y1
        xi += x1
        layer[yi, xi] = np.array([x1, y1, x2, y2])
    return layer


def register_note_id():
    symbols = layers.get_layer('symbols_pred')
    layer = layers.get_layer('note_id')
    notes = layers.get_layer('notes')
    for idx, note in enumerate(notes):
        x1, y1, x2, y2 = note.bbox
        yi, xi = np.where(symbols[y1:y2, x1:x2]>0)
        yi += y1
        xi += x1
        layer[yi, xi] = idx
        notes[idx].id = idx


def extract(args):
    img_path = Path(args.img_path)
    f_name = os.path.splitext(img_path.name)[0]
    pkl_path = img_path.parent / f"{f_name}.pkl"
    if pkl_path.exists():
        # Load from cache
        pred = pickle.load(open(pkl_path, "rb"))
        notehead = pred["note"]
        symbols = pred["symbols"]
        staff = pred["staff"]
        clefs_keys = pred["clefs_keys"]
        stems_rests = pred["stems_rests"]
    else:
        # Make predictions
        staff, symbols, stems_rests, notehead, clefs_keys = generate_pred(str(img_path))
        if args.save_cache:
            data = {
                'staff': staff,
                'note': notehead,
                'symbols': symbols,
                'stems_rests': stems_rests,
                'clefs_keys': clefs_keys
            }
            pickle.dump(data, open(pkl_path, "wb"))

    # Load the original image, resize to the same size as prediction.
    image = cv2.imread(str(img_path))
    image = cv2.resize(image, (staff.shape[1], staff.shape[0]))

    logger.info("Dewarping")
    coords_x, coords_y = estimate_coords(staff)
    staff = dewarp(staff, coords_x, coords_y)
    symbols = dewarp(symbols, coords_x, coords_y)
    stems_rests = dewarp(stems_rests, coords_x, coords_y)
    clefs_keys = dewarp(clefs_keys, coords_x, coords_y)
    notehead = dewarp(notehead, coords_x, coords_y)
    for i in range(image.shape[2]):
        image[..., i] = dewarp(image[..., i], coords_x, coords_y)

    # Register predictions
    symbols = symbols + clefs_keys + stems_rests
    symbols[symbols>1] = 1
    layers.register_layer("stems_rests_pred", stems_rests)
    layers.register_layer("clefs_keys_pred", clefs_keys)
    layers.register_layer("notehead_pred", notehead)
    layers.register_layer("symbols_pred", symbols)
    layers.register_layer("staff_pred", staff)
    layers.register_layer("original_image", image)

    # ---- Extract staff lines and group informations ---- #
    logger.info("Extracting stafflines")
    staffs, zones = staff_extract()
    layers.register_layer("staffs", staffs)  # Array of 'Staff' instances
    layers.register_layer("zones", zones)  # Range of each zones, array of 'range' object.

    # ---- Extract noteheads ---- #
    logger.info("Extracting noteheads")
    notes = note_extract()

    # Add a new layer (w * h * 4), indicating bbox of each pixel.
    layers.register_layer('bboxes', np.zeros(symbols.shape + (4,), dtype=np.uint8))
    bboxes = [note.bbox for note in notes]
    register_notehead_bbox(bboxes)

    # Array of 'NoteHead' instances.
    layers.register_layer('notes', np.array(notes))

    # Add a new layer (w * h), indicating note id of each pixel.
    layers.register_layer('note_id', np.zeros(symbols.shape, dtype=int)-1)
    register_note_id()

    # ---- Extract groups of note ---- #
    logger.info("Grouping noteheads")
    groups, group_map = group_extract()
    layers.register_layer('note_groups', np.array(groups))
    layers.register_layer('group_map', group_map)

    # ---- Extract symbols ---- #
    logger.info("Extracting symbols")
    barlines, clefs, sfns, rests = symbol_extract()
    layers.register_layer('barlines', np.array(barlines))
    layers.register_layer('clefs', np.array(clefs))
    layers.register_layer('sfns', np.array(sfns))
    layers.register_layer('rests', np.array(rests))

    # ---- Parse rhythm ---- #
    logger.info("Extracting rhythm types")
    rhythm_extract()

    # ---- Build MusicXML ---- #
    logger.info("Building MusicXML document")
    basename = os.path.basename(img_path).replace(".jpg", "").replace(".png", "")
    builder = MusicXMLBuilder(title=basename.capitalize())
    builder.build()
    xml = builder.to_musicxml()
    with open(f"./{basename}.musicxml", "wb") as ff:
        ff.write(xml)


def main():
    parser = argparse.ArgumentParser("Oemer", description="End-to-end OMR")
    parser.add_argument("img_path", help="Path to the image.", type=str)
    parser.add_argument(
        "--save-cache",
        help="Save the model predictions and the next time won't need to predict again.",
        action='store_true')

    args = parser.parse_args()

    if not os.path.exists(args.img_path):
        raise FileNotFoundError(f"The given image path doesn't exists: {args.img_path}")

    clear_data()
    extract(args)


if __name__ == "__main__":
    main()