"""OpenCV color-distribution and spatial-light comparison for CLIP reranking."""
import io
import numpy as np


def describe(content):
    import cv2
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(content)) as image:
        if image.format == 'JPEG':
            image.draft('RGB', (256, 256))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        pixels = np.array(image.convert('RGB'))
    lab = cv2.cvtColor(pixels, cv2.COLOR_RGB2LAB)
    histogram = cv2.calcHist([lab], [0, 1, 2], None, [8, 8, 8], [0, 256]*3)
    # Neighboring perceptual colors must overlap across quantization edges.
    histogram = cv2.GaussianBlur(histogram, (3, 3), .65, borderType=cv2.BORDER_REPLICATE)
    histogram = cv2.GaussianBlur(histogram.transpose(2, 1, 0).copy(), (3, 3), .65,
                                 borderType=cv2.BORDER_REPLICATE).transpose(2, 1, 0).copy()
    histogram = cv2.normalize(histogram, None, alpha=1., norm_type=cv2.NORM_L1)
    spatial = cv2.resize(lab, (8, 8), interpolation=cv2.INTER_AREA).astype(np.float32)/255.
    return histogram.flatten().astype(np.float32), spatial.flatten()


def compare(query, candidate, semantic):
    import cv2
    color = max(0., 1.-cv2.compareHist(query[0], candidate[0], cv2.HISTCMP_BHATTACHARYYA))
    layout = float(np.exp(-8.*np.mean(np.abs(query[1]-candidate[1]))))
    return dict(score=.55*semantic+.30*color+.15*layout,
                content_score=semantic, color_score=color, layout_score=layout)
