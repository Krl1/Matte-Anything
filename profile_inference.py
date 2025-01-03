import os
import io
import sys
import cv2
import time
import torch
import pstats
import cProfile
import argparse
import numpy as np
import gradio as gr
from PIL import Image
from re import findall
from pstats import SortKey
from torchvision.ops import box_convert
from detectron2.config import LazyConfig, instantiate
from detectron2.checkpoint import DetectionCheckpointer
from segment_anything import sam_model_registry, SamPredictor
import groundingdino.datasets.transforms as T
from groundingdino.util.inference import (
    load_model as dino_load_model,
    predict as dino_predict,
    annotate as dino_annotate,
)


MATTING_MODELS = ["ViTMatte", "DiffMatte", "AEMatter"]
MATTING_IDX = 0

models = {
    "vit_h": "./pretrained/sam_vit_h_4b8939.pth",
    "vit_b": "./pretrained/sam_vit_b_01ec64.pth",
}

vitmatte_models = {
    "vit_b": "./pretrained/ViTMatte_B_DIS.pth",
}

vitmatte_config = {
    "vit_b": "./configs/matte_anything.py",
}

grounding_dino = {
    "config": "./GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py",
    "weight": "./pretrained/groundingdino_swint_ogc.pth",
}


def profile_function(func, matte_method, *args, **kwargs):
    """
    Profiles a function and prints the results.

    Args:
        func: The function to profile.
        *args: Positional arguments for the function.
        **kwargs: Keyword arguments for the function.
    """
    profiler = cProfile.Profile()
    profiler.enable()
    result = func(*args, **kwargs)
    profiler.disable()

    directory_path = f"profiling_results/{matte_method}"
    os.makedirs(directory_path, exist_ok=True)
    profiler.dump_stats(f'{directory_path}/{time.strftime("%Y%m%d-%H%M%S")}.prof')
    s = io.StringIO()
    ps = pstats.Stats(profiler, stream=s).sort_stats(SortKey.CUMULATIVE)
    ps.print_stats("predictor.py:", 1)
    ps.print_stats("inference.py:", 1)
    ps.print_stats("matte_anything.py:", 3)
    print(s.getvalue())
    return result


def generate_checkerboard_image(height, width, num_squares):
    num_squares_h = num_squares
    square_size_h = height // num_squares_h
    square_size_w = square_size_h
    num_squares_w = width // square_size_w

    new_height = num_squares_h * square_size_h
    new_width = num_squares_w * square_size_w
    image = np.zeros((new_height, new_width), dtype=np.uint8)

    for i in range(num_squares_h):
        for j in range(num_squares_w):
            start_x = j * square_size_w
            start_y = i * square_size_h
            color = 255 if (i + j) % 2 == 0 else 200
            image[
                start_y : start_y + square_size_h, start_x : start_x + square_size_w
            ] = color

    image = cv2.resize(image, (width, height))
    image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)

    return image


def generate_white_background(height, width):
    image = np.ones((height, width, 3), dtype=np.uint8) * 255
    return image


def init_segment_anything(model_type):
    """
    Initialize the segmenting anything with model_type in ['vit_b', 'vit_l', 'vit_h']
    """

    sam = sam_model_registry[model_type](checkpoint=models[model_type]).to(device)
    predictor = SamPredictor(sam)

    return predictor


def init_matte(matte_method, vitmatte_model):
    if matte_method == "ViTMatte":
        return init_vitmatte(vitmatte_model)
    elif matte_method == "DiffMatte":
        return init_diffmatte()
    elif matte_method == "AEMatter":
        return init_aematter()
    else:
        raise ValueError("Unknown matting model")


def init_vitmatte(model_type):
    """
    Initialize the vitmatte with model_type in ['vit_s', 'vit_b']
    """
    cfg = LazyConfig.load(vitmatte_config[model_type])
    vitmatte = instantiate(cfg.model)
    vitmatte.to(device)
    vitmatte.eval()
    DetectionCheckpointer(vitmatte).load(vitmatte_models[model_type])

    return vitmatte


def init_diffmatte(
    model="./DiffMatte/configs/ViTS_1024.py",
    checkpoint="./pretrained/DiffMatte_ViTS_Com_1024.pth",
    sample_strategy="ddim10",
):

    diffmatte_path = os.path.join(os.path.dirname(__file__), "DiffMatte")
    sys.path.insert(0, diffmatte_path)

    cfg = LazyConfig.load(model)
    if sample_strategy is not None:
        cfg.difmatte.args["use_ddim"] = True if "ddim" in sample_strategy else False
        cfg.diffusion.steps = int(findall(r"\d+", sample_strategy)[0])

    model = instantiate(cfg.model)
    diffusion = instantiate(cfg.diffusion)
    cfg.difmatte.model = model
    cfg.difmatte.diffusion = diffusion
    difmatte = instantiate(cfg.difmatte)
    difmatte.to(device)
    difmatte.eval()
    DetectionCheckpointer(difmatte).load(checkpoint)

    return difmatte


def init_aematter(
    checkpoint="./pretrained/AEMFIX.ckpt",
):
    aematte_path = os.path.join(os.path.dirname(__file__), "AEMatter")
    sys.path.insert(0, aematte_path)

    from model import AEMatter

    aematter = AEMatter()
    aematter.load_state_dict(torch.load(checkpoint, map_location="cpu")["model"])
    aematter = aematter.to(device)
    aematter.eval()

    return aematter


def generate_trimap(mask, erode_kernel_size=10, dilate_kernel_size=10):
    erode_kernel = np.ones((erode_kernel_size, erode_kernel_size), np.uint8)
    dilate_kernel = np.ones((dilate_kernel_size, dilate_kernel_size), np.uint8)
    eroded = cv2.erode(mask, erode_kernel, iterations=5)
    dilated = cv2.dilate(mask, dilate_kernel, iterations=5)
    trimap = np.zeros_like(mask)
    trimap[dilated == 255] = 128
    trimap[eroded == 255] = 255
    return trimap


# user click the image to get points, and show the points on the image
def get_point(img, sel_pix, point_type, evt: gr.SelectData):
    if point_type == "foreground_point":
        sel_pix.append((evt.index, 1))  # append the foreground_point
    elif point_type == "background_point":
        sel_pix.append((evt.index, 0))  # append the background_point
    else:
        sel_pix.append((evt.index, 1))  # default foreground_point
    # draw points
    for point, label in sel_pix:
        cv2.drawMarker(
            img,
            point,
            colors[label],
            markerType=markers[label],
            markerSize=20,
            thickness=5,
        )
    if img[..., 0][0, 0] == img[..., 2][0, 0]:  # BGR to RGB
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return img if isinstance(img, np.ndarray) else np.array(img)


# undo the selected point
def undo_points(orig_img, sel_pix):
    temp = orig_img.copy()
    # draw points
    if len(sel_pix) != 0:
        sel_pix.pop()
        for point, label in sel_pix:
            cv2.drawMarker(
                temp,
                point,
                colors[label],
                markerType=markers[label],
                markerSize=20,
                thickness=5,
            )
    if temp[..., 0][0, 0] == temp[..., 2][0, 0]:  # BGR to RGB
        temp = cv2.cvtColor(temp, cv2.COLOR_BGR2RGB)
    return temp if isinstance(temp, np.ndarray) else np.array(temp)


# undo all selected points
def undo_all_points(orig_img, sel_pix):
    if orig_img is None:
        raise gr.Error("Please upload pictures first!")
    else:
        temp = orig_img.copy()
        while len(sel_pix) != 0:
            sel_pix.pop()
        if temp[..., 0][0, 0] == temp[..., 2][0, 0]:  # BGR to RGB
            temp = cv2.cvtColor(temp, cv2.COLOR_BGR2RGB)
    return temp if isinstance(temp, np.ndarray) else np.array(temp)


# clear the fg_caption
def clear_fg_caption(fg_caption):
    fg_caption = ""
    return fg_caption


# once user upload an image, the original image is stored in `original_image`
def store_img(img):
    return img, []  # when new image is uploaded, `selected_points` should be empty


def convert_pixels(gray_image, boxes):
    converted_image = np.copy(gray_image)

    for box in boxes:
        x1, y1, x2, y2 = box
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        converted_image[y1:y2, x1:x2][converted_image[y1:y2, x1:x2] == 1] = 0.5

    return converted_image


def pred_matting(model, input_x, trimap):
    input = {
        "image": torch.from_numpy(input_x).permute(2, 0, 1).unsqueeze(0) / 255,
        "trimap": torch.from_numpy(trimap).unsqueeze(0).unsqueeze(0),
    }

    if model.__class__.__name__ == "ViTMatte":
        alpha = model(input)
        alpha = alpha["phas"].flatten(0, 2)
        alpha = alpha.detach().cpu().numpy()
    elif model.__class__.__name__ == "DifMatte":
        alpha = model(input)
        alpha /= 255.0
    elif model.__class__.__name__ == "AEMatter":
        trimap = np.array(trimap * 255, np.uint8)
        trimap[trimap == 127] = 128
        trimap_nonp = trimap.copy()
        image, trimap, sizes = preprocess_input(input_x, trimap)
        with torch.no_grad():
            alpha = model(image, trimap)
            alpha = postprocess_alpha(alpha, trimap_nonp, sizes)
    return alpha


def preprocess_input(rawimg, trimap):
    h, w, c = rawimg.shape
    nonph, nonpw, _ = rawimg.shape
    newh = (((h - 1) // 32) + 1) * 32
    neww = (((w - 1) // 32) + 1) * 32
    padh = newh - h
    padh1 = int(padh / 2)
    padh2 = padh - padh1
    padw = neww - w
    padw1 = int(padw / 2)
    padw2 = padw - padw1
    rawimg_pad = cv2.copyMakeBorder(
        rawimg, padh1, padh2, padw1, padw2, cv2.BORDER_REFLECT
    )
    trimap_pad = cv2.copyMakeBorder(
        trimap, padh1, padh2, padw1, padw2, cv2.BORDER_REFLECT
    )
    h_pad, w_pad, _ = rawimg_pad.shape
    tritemp = np.zeros([*trimap_pad.shape, 3], np.float32)
    tritemp[:, :, 0] = trimap_pad == 0
    tritemp[:, :, 1] = trimap_pad == 128
    tritemp[:, :, 2] = trimap_pad == 255
    tritempimgs = np.transpose(tritemp, (2, 0, 1))
    tritempimgs = tritempimgs[np.newaxis, :, :, :]
    img = np.transpose(rawimg_pad, (2, 0, 1))[np.newaxis, ::-1, :, :]
    img = np.array(img, np.float32)
    img = img / 255.0
    img = torch.from_numpy(img).to(device)
    tritempimgs = torch.from_numpy(tritempimgs).to(device)
    sizes = {"h": h, "w": w, "padh1": padh1, "padw1": padw1}
    return img, tritempimgs, sizes


def postprocess_alpha(pred, trimap_nonp, sizes):
    h, w, padh1, padw1 = sizes["h"], sizes["w"], sizes["padh1"], sizes["padw1"]
    pred = pred.detach().cpu().numpy()[0]
    pred = pred[:, padh1 : padh1 + h, padw1 : padw1 + w]
    preda = pred[0:1,] * 255
    preda = np.transpose(preda, (1, 2, 0))
    preda = (
        preda * (trimap_nonp[:, :, None] == 128)
        + (trimap_nonp[:, :, None] == 255) * 255
    )
    preda /= 255.0
    return preda.squeeze()


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--matte-method",
        "-m",
        type=str,
        default="ViTMatte",
        choices=["ViTMatte", "DiffMatte", "AEMatter"],
        help="Matting method to use (default: 'ViTMatte')",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()

    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    sam_model = "vit_h"
    vitmatte_model = "vit_b"

    colors = [(255, 0, 0), (0, 255, 0)]
    markers = [1, 5]

    print("Initializing models... Please wait...")

    predictor = init_segment_anything(sam_model)
    matting_model = init_matte(args.matte_method, vitmatte_model)
    grounding_dino = dino_load_model(grounding_dino["config"], grounding_dino["weight"])

    def run_inference(
        input_x,
        selected_points,
        erode_kernel_size,
        dilate_kernel_size,
        fg_box_threshold,
        fg_text_threshold,
        fg_caption,
        tr_box_threshold,
        tr_text_threshold,
        save_name,
        tr_caption="glass, lens, crystal, diamond, bubble, bulb, web, grid",
    ):

        if len(selected_points) == 0:
            selected_points.append(([input_x.shape[1] // 2, input_x.shape[0] // 2], 1))

        if fg_caption is None or fg_caption == "":
            fg_caption = "the biggest foreground object"

        predictor.set_image(input_x)

        dino_transform = T.Compose(
            [
                T.RandomResize([800], max_size=1333),
                T.ToTensor(),
                T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ]
        )
        image_transformed, _ = dino_transform(Image.fromarray(input_x), None)

        points = torch.Tensor([p for p, _ in selected_points]).to(device).unsqueeze(1)
        labels = (
            torch.Tensor([int(l) for _, l in selected_points]).to(device).unsqueeze(1)
        )
        transformed_points = predictor.transform.apply_coords_torch(
            points, input_x.shape[:2]
        )
        print(
            points.size(),
            transformed_points.size(),
            labels.size(),
            input_x.shape,
            points,
        )
        point_coords = transformed_points.permute(1, 0, 2)
        point_labels = labels.permute(1, 0)

        fg_boxes, logits, phrases = dino_predict(
            model=grounding_dino,
            image=image_transformed,
            caption=fg_caption,
            box_threshold=fg_box_threshold,
            text_threshold=fg_text_threshold,
            device=device,
        )

        print(logits, phrases, fg_boxes)
        if len(phrases) > 1:
            max_logit_index = torch.argmax(logits)
            logits = logits[max_logit_index]
            phrases = phrases[max_logit_index]
            fg_boxes = fg_boxes[max_logit_index]

        if fg_boxes.shape[0] == 0:
            # no fg object detected
            transformed_boxes = None
        else:
            h, w, _ = input_x.shape
            fg_boxes = torch.Tensor(fg_boxes).to(device)
            fg_boxes = fg_boxes * torch.Tensor([w, h, w, h]).to(device)
            fg_boxes = box_convert(boxes=fg_boxes, in_fmt="cxcywh", out_fmt="xyxy")
            transformed_boxes = predictor.transform.apply_boxes_torch(
                fg_boxes, input_x.shape[:2]
            )

        # predict segmentation according to the boxes
        masks, scores, logits = predictor.predict_torch(
            point_coords=point_coords,
            point_labels=point_labels,
            boxes=transformed_boxes,
            multimask_output=False,
        )
        masks = masks.cpu().detach().numpy()
        mask_all = np.ones((input_x.shape[0], input_x.shape[1], 3))
        for ann in masks:
            color_mask = np.random.random((1, 3)).tolist()[0]
            for i in range(3):
                mask_all[ann[0] == True, i] = color_mask[i]
        img = input_x / 255 * 0.3 + mask_all * 0.7

        # generate alpha matte
        torch.cuda.empty_cache()
        mask = masks[0][0].astype(np.uint8) * 255
        trimap = generate_trimap(mask, erode_kernel_size, dilate_kernel_size).astype(
            np.float32
        )
        trimap[trimap == 128] = 0.5
        trimap[trimap == 255] = 1

        boxes, logits, phrases = dino_predict(
            model=grounding_dino,
            image=image_transformed,
            caption=tr_caption,
            box_threshold=tr_box_threshold,
            text_threshold=tr_text_threshold,
            device=device,
        )
        annotated_frame = dino_annotate(
            image_source=input_x, boxes=boxes, logits=logits, phrases=phrases
        )

        annotated_frame = cv2.cvtColor(annotated_frame, cv2.COLOR_BGR2RGB)

        if boxes.shape[0] == 0:
            # no transparent object detected
            pass
        else:
            h, w, _ = input_x.shape
            boxes = boxes * torch.Tensor([w, h, w, h])
            xyxy = box_convert(boxes=boxes, in_fmt="cxcywh", out_fmt="xyxy").numpy()
            trimap = convert_pixels(trimap, xyxy)

        torch.cuda.empty_cache()
        alpha = pred_matting(matting_model, input_x, trimap)

        # get a green background
        # background = generate_checkerboard_image(input_x.shape[0], input_x.shape[1], 8)
        background = generate_white_background(input_x.shape[0], input_x.shape[1])
        # calculate foreground with alpha blending
        foreground_alpha = (
            input_x * np.expand_dims(alpha, axis=2).repeat(3, 2) / 255
            + background * (1 - np.expand_dims(alpha, axis=2).repeat(3, 2)) / 255
        )

        # calculate foreground with mask
        foreground_mask = (
            input_x * np.expand_dims(mask / 255, axis=2).repeat(3, 2) / 255
            + background * (1 - np.expand_dims(mask / 255, axis=2).repeat(3, 2)) / 255
        )

        # concatenate input_x and foreground_alpha
        cv2_alpha = (np.expand_dims(alpha, axis=2) * 255).astype(np.uint8)
        cv2_input_x = cv2.cvtColor(input_x, cv2.COLOR_BGR2RGB)
        rgba = np.concatenate((cv2_input_x, cv2_alpha), axis=2)
        cv2.imwrite(f"your_demos/{save_name}.png", rgba)

        foreground_alpha[foreground_alpha > 1] = 1
        foreground_mask[foreground_mask > 1] = 1

        # return img, mask_all
        trimap[trimap == 1] == 0.999

        # new background

        background_1 = cv2.imread("figs/sea.jpg")
        background_2 = cv2.imread("figs/forest.jpg")
        background_3 = cv2.imread("figs/sunny.jpg")

        background_1 = cv2.resize(background_1, (input_x.shape[1], input_x.shape[0]))
        background_2 = cv2.resize(background_2, (input_x.shape[1], input_x.shape[0]))
        background_3 = cv2.resize(background_3, (input_x.shape[1], input_x.shape[0]))

        # to RGB
        background_1 = cv2.cvtColor(background_1, cv2.COLOR_BGR2RGB)
        background_2 = cv2.cvtColor(background_2, cv2.COLOR_BGR2RGB)
        background_3 = cv2.cvtColor(background_3, cv2.COLOR_BGR2RGB)

        # use alpha blending
        new_bg_1 = (
            input_x * np.expand_dims(alpha, axis=2).repeat(3, 2) / 255
            + background_1 * (1 - np.expand_dims(alpha, axis=2).repeat(3, 2)) / 255
        )
        new_bg_2 = (
            input_x * np.expand_dims(alpha, axis=2).repeat(3, 2) / 255
            + background_2 * (1 - np.expand_dims(alpha, axis=2).repeat(3, 2)) / 255
        )
        new_bg_3 = (
            input_x * np.expand_dims(alpha, axis=2).repeat(3, 2) / 255
            + background_3 * (1 - np.expand_dims(alpha, axis=2).repeat(3, 2)) / 255
        )

        return (
            mask,
            alpha,
            foreground_mask,
            foreground_alpha,
            new_bg_1,
            new_bg_2,
            new_bg_3,
        )

    # Profiling wrapper for the Gradio callback
    def profiled_run_inference(
        input_x,
        selected_points,
        erode_kernel_size,
        dilate_kernel_size,
        fg_box_threshold,
        fg_text_threshold,
        fg_caption,
        tr_box_threshold,
        tr_text_threshold,
        save_name,
        tr_caption="glass, lens, crystal, diamond, bubble, bulb, web, grid",
    ):
        return profile_function(
            run_inference,
            args.matte_method,
            input_x,
            selected_points,
            erode_kernel_size,
            dilate_kernel_size,
            fg_box_threshold,
            fg_text_threshold,
            fg_caption,
            tr_box_threshold,
            tr_text_threshold,
            save_name,
            tr_caption="glass, lens, crystal, diamond, bubble, bulb, web, grid",
        )

    with gr.Blocks() as demo:
        gr.Markdown(
            """
            # <center>Matte Anything🐒 !
            """
        )
        with gr.Row(equal_height=True):  # .style(equal_height=True):
            with gr.Column():
                # input image
                original_image = gr.State(
                    value="numpy"
                )  # store original image without points, default None
                input_image = gr.Image(type="numpy", label="Input Image")
                # prompt (point or text)
                # Point Input
                with gr.Tab(label="Point Input") as Tab1:
                    with gr.Column():
                        selected_points = gr.State([])  # store points
                        radio = gr.Radio(
                            ["foreground_point", "background_point"],
                            label="Point Labels",
                        )
                        with gr.Row():
                            undo_button = gr.Button("Remove Point")
                            undo_all_button = gr.Button("Remove All  Points")
                # Foreground Text Input
                with gr.Tab(label="Foreground Text Input") as Tab2:
                    with gr.Blocks():
                        gr.Markdown("Foreground Text Input")
                        fg_caption = gr.Textbox(
                            lines=1,
                            placeholder="the girl in the middle",
                            label="foreground input text",
                        )

                # Save Config
                with gr.Tab(label="Save Config") as Tab3:
                    with gr.Blocks():
                        gr.Markdown("save name")
                        save_dir = gr.Textbox(
                            lines=1,
                            placeholder="the girl in the middle",
                            label="Give a name of your demo. It will be saved in ```your_demos/your_name.pny```",
                        )

                # run button
                button = gr.Button("Start!")

                # Trimap Settings
                with gr.Tab(label="Trimap Settings"):
                    gr.Markdown("Trimap Settings")
                    erode_kernel_size = gr.Slider(
                        minimum=1,
                        maximum=30,
                        step=1,
                        value=10,
                        label="erode_kernel_size",
                    )
                    dilate_kernel_size = gr.Slider(
                        minimum=1,
                        maximum=30,
                        step=1,
                        value=10,
                        label="dilate_kernel_size",
                    )

                # Input Text Settings
                with gr.Tab(label="Input Text Settings"):
                    gr.Markdown("Input Text Settings")
                    fg_box_threshold = gr.Slider(
                        minimum=0.0,
                        maximum=1.0,
                        step=0.001,
                        value=0.25,
                        label="foreground_box_threshold",
                    )
                    fg_text_threshold = gr.Slider(
                        minimum=0.0,
                        maximum=1.0,
                        step=0.001,
                        value=0.25,
                        label="foreground_text_threshold",
                    )

                # Transparency Settings
                with gr.Tab(label="Transparency Settings"):
                    gr.Markdown("Transparency Settings")
                    tr_caption = gr.Textbox(
                        lines=1,
                        placeholder="glass.lens.crystal.diamond.bubble.bulb.web.grid",
                        label="transparency input text",
                    )
                    tr_box_threshold = gr.Slider(
                        minimum=0.0,
                        maximum=1.0,
                        step=0.005,
                        value=0.5,
                        label="transparency_box_threshold",
                    )
                    tr_text_threshold = gr.Slider(
                        minimum=0.0,
                        maximum=1.0,
                        step=0.005,
                        value=0.25,
                        label="transparency_text_threshold",
                    )

            with gr.Column():

                # show the image with mask
                with gr.Tab(label="SAM Mask"):
                    mask = gr.Image(type="numpy")
                # with gr.Tab(label='Trimap'):
                #     trimap = gr.Image(type='numpy')
                with gr.Tab(label="Alpha Matte"):
                    alpha = gr.Image(type="numpy")
                # show only mask
                with gr.Tab(label="Foreground by SAM Mask"):
                    foreground_by_sam_mask = gr.Image(type="numpy")
                with gr.Tab(label="Refined by ViTMatte"):
                    refined_by_vitmatte = gr.Image(type="numpy")
                # with gr.Tab(label='Transparency Detection'):
                #     transparency = gr.Image(type='numpy')
                with gr.Tab(label="New Background 1"):
                    new_bg_1 = gr.Image(type="numpy")
                with gr.Tab(label="New Background 2"):
                    new_bg_2 = gr.Image(type="numpy")
                with gr.Tab(label="New Background 3"):
                    new_bg_3 = gr.Image(type="numpy")

        input_image.upload(store_img, [input_image], [original_image, selected_points])
        input_image.select(
            get_point,
            [input_image, selected_points, radio],
            [input_image],
        )
        undo_button.click(undo_points, [original_image, selected_points], [input_image])
        undo_all_button.click(
            undo_all_points, [original_image, selected_points], [input_image]
        )
        Tab1.select(clear_fg_caption, [fg_caption], [fg_caption])
        Tab2.select(undo_all_points, [original_image, selected_points], [input_image])

        button.click(
            profiled_run_inference,
            inputs=[
                original_image,
                selected_points,
                erode_kernel_size,
                dilate_kernel_size,
                fg_box_threshold,
                fg_text_threshold,
                fg_caption,
                tr_box_threshold,
                tr_text_threshold,
                save_dir,
                tr_caption,
            ],
            outputs=[
                mask,
                alpha,
                foreground_by_sam_mask,
                refined_by_vitmatte,
                new_bg_1,
                new_bg_2,
                new_bg_3,
            ],
        )

        with gr.Row():
            with gr.Column():
                background_image = gr.State(value=None)

    demo.launch(server_name="0.0.0.0", server_port=7860)
