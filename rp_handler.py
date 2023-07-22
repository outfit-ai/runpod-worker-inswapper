import os
import io
import uuid
import base64
import copy
import cv2
import insightface
import numpy as np
import runpod
from runpod.serverless.utils.rp_validator import validate
from runpod.serverless.modules.rp_logger import RunPodLogger
from typing import List, Union
from PIL import Image
from restoration import *

TMP_PATH = '/tmp/inswapper'
script_dir = os.path.dirname(os.path.abspath(__file__))
logger = RunPodLogger()

INPUT_SCHEMA = {
    'source_image': {
        'type': str,
        'required': True
    },
    'target_image': {
        'type': str,
        'required': True
    },
    'target_index': {
        'type': int,
        'required': False,
        'default': -1  # Default to swapping all faces in the target image
    }
}


# ---------------------------------------------------------------------------- #
# Application Functions                                                        #
# ---------------------------------------------------------------------------- #
def get_face_swap_model(model_path: str):
    model = insightface.model_zoo.get_model(model_path)
    return model


def get_face_analyser(model_path: str,
                      det_size=(320, 320)):
    face_analyser = insightface.app.FaceAnalysis(name="buffalo_l", root="./checkpoints")
    face_analyser.prepare(ctx_id=0, det_size=det_size)
    return face_analyser


def get_one_face(face_analyser,
                 frame:np.ndarray):
    face = face_analyser.get(frame)
    try:
        return min(face, key=lambda x: x.bbox[0])
    except ValueError:
        return None


def get_many_faces(face_analyser,
                   frame:np.ndarray):
    """
    get faces from left to right by order
    """
    try:
        face = face_analyser.get(frame)
        return sorted(face, key=lambda x: x.bbox[0])
    except IndexError:
        return None


def swap_face(face_swapper,
              face_analyser,
              source_img,
              target_faces,
              source_index,
              target_index,
              temp_frame):
    """
    paste source_face on target image
    """
    target_face = target_faces[target_index]
    source_face = get_one_face(face_analyser, cv2.cvtColor(np.array(source_img[source_index]), cv2.COLOR_RGB2BGR))
    if source_face is None:
        logger.error('No source face found')
        raise Exception('No source face found!')

    return face_swapper.get(temp_frame, target_face, source_face, paste_back=True)


def process(source_img: Union[Image.Image, List],
            target_img: Image.Image,
            target_index: int,
            model: str):

    # load face_analyser
    face_analyser = get_face_analyser(model)

    # load face_swapper
    model_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), model)
    face_swapper = get_face_swap_model(model_path)

    # read target image
    target_img = cv2.cvtColor(np.array(target_img), cv2.COLOR_RGB2BGR)

    # detect faces that will be replaced in target_img
    target_faces = get_many_faces(face_analyser, target_img)
    if target_faces is not None:
        temp_frame = copy.deepcopy(target_img)
        if isinstance(source_img, list) and len(source_img) == len(target_faces):
            logger.info('Replacing the faces in the target image from left to right by order')
            for i in range(len(target_faces)):
                source_index = i
                target_index = i

                temp_frame = swap_face(
                    face_swapper,
                    face_analyser,
                    source_img,
                    target_faces,
                    source_index,
                    target_index,
                    temp_frame
                )
        else:
            if target_index == -1:
                logger.info('Replacing all faces in the target image to the face in the source image')
                for i in range(len(target_faces)):
                    source_index = 0
                    target_index = i

                    temp_frame = swap_face(
                        face_swapper,
                        face_analyser,
                        source_img,
                        target_faces,
                        source_index,
                        target_index,
                        temp_frame
                    )
            else:
                logger.info(f'Replacing the face at index {target_index} in the target image')
                source_index = 0

                temp_frame = swap_face(
                    face_swapper,
                    face_analyser,
                    source_img,
                    target_faces,
                    source_index,
                    target_index,
                    temp_frame
                )
        result = temp_frame
    else:
        logger.error('No target faces found')
        raise Exception('No target faces found!')

    result_image = Image.fromarray(cv2.cvtColor(result, cv2.COLOR_BGR2RGB))
    return result_image


def face_swap(src_img_path, target_img_path, target_index):
    source_img_paths = src_img_path.split(';')
    source_img = [Image.open(img_path) for img_path in source_img_paths]
    target_img = Image.open(target_img_path)

    # download from https://huggingface.co/deepinsight/inswapper/tree/main
    model = os.path.join(script_dir, 'checkpoints/inswapper_128.onnx')
    logger.info(f'Face swap mode: {model}')

    try:
        logger.info('Performing face swap')
        result_image = process(source_img, target_img, target_index, model)
        logger.info('Face swap complete')
    except Exception as e:
        raise

    # make sure the ckpts downloaded successfully
    check_ckpts()

    # https://huggingface.co/spaces/sczhou/CodeFormer
    logger.info('Setting upsampler to RealESRGAN_x2plus')
    upsampler = set_realesrgan()

    if torch.cuda.is_available():
        torch_device = 'cuda'
    else:
        torch_device = 'cpu'

    logger.info(f'Torch device: {torch_device.upper()}')
    device = torch.device(torch_device)

    codeformer_net = ARCH_REGISTRY.get('CodeFormer')(
        dim_embd=512,
        codebook_size=1024,
        n_head=8,
        n_layers=9,
        connect_list=['32', '64', '128', '256'],
    ).to(device)

    ckpt_path = os.path.join(script_dir, 'CodeFormer/CodeFormer/weights/CodeFormer/codeformer.pth')
    logger.info(f'Loading CodeFormer model: {ckpt_path}')
    checkpoint = torch.load(ckpt_path)['params_ema']
    codeformer_net.load_state_dict(checkpoint)
    codeformer_net.eval()
    result_image = cv2.cvtColor(np.array(result_image), cv2.COLOR_RGB2BGR)
    background_enhance = True
    face_upsample = True
    upscale = 1
    codeformer_fidelity = 0.5
    logger.info('Performing face restoration using CodeFormer')

    try:
        result_image = face_restoration(
            result_image,
            background_enhance,
            face_upsample,
            upscale,
            codeformer_fidelity,
            upsampler,
            codeformer_net,
            device
        )
    except Exception as e:
        raise

    logger.info('CodeFormer face restoration completed successfully')
    result_image = Image.fromarray(result_image)
    output_buffer = io.BytesIO()
    result_image.save(output_buffer, format='JPEG')
    image_data = output_buffer.getvalue()

    return base64.b64encode(image_data).decode('utf-8')


def determine_file_extension(image_data):
    image_extension = None

    try:
        if image_data.startswith('/9j/'):
            image_extension = '.jpg'
        elif image_data.startswith('iVBORw0Kg'):
            image_extension = '.png'
        else:
            # Default to png if we can't figure out the extension
            image_extension = '.png'
    except Exception as e:
        image_extension = '.png'

    return image_extension


def face_swap_api(input):
    if not os.path.exists(TMP_PATH):
        os.makedirs(TMP_PATH)

    unique_id = uuid.uuid4()
    source_image_data = input['source_image']
    target_image_data = input['target_image']
    target_index = input['target_index']

    # Decode the source image data
    source_image = base64.b64decode(source_image_data)
    source_file_extension = determine_file_extension(source_image_data)
    source_image_path = f'{TMP_PATH}/source_{unique_id}{source_file_extension}'

    # Save the source image to disk
    with open(source_image_path, 'wb') as source_file:
        source_file.write(source_image)

    # Decode the target image data
    target_image = base64.b64decode(target_image_data)
    target_file_extension = determine_file_extension(target_image_data)
    target_image_path = f'{TMP_PATH}/target_{unique_id}{target_file_extension}'

    # Save the target image to disk
    with open(target_image_path, 'wb') as target_file:
        target_file.write(target_image)

    try:
        result_image = face_swap(
            source_image_path,
            target_image_path,
            target_index
        )
    except Exception as e:
        return {
            'status': 'error',
            'message': str(e)
        }

    # Clean up temporary images
    os.remove(source_image_path)
    os.remove(target_image_path)

    return {
        'status': 'ok',
        'image': result_image
    }


# ---------------------------------------------------------------------------- #
# RunPod Handler                                                               #
# ---------------------------------------------------------------------------- #
def handler(event):
    validated_input = validate(event['input'], INPUT_SCHEMA)

    if 'errors' in validated_input:
        return {
            'errors': validated_input['errors']
        }

    return face_swap_api(validated_input['validated_input'])


if __name__ == "__main__":
    logger.info('Starting RunPod Serverless...')
    runpod.serverless.start(
        {
            'handler': handler
        }
    )