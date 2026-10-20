"""File classification shared by drop/import flows; no GUI or renderer imports.

Editors may support a smaller display subset. A supported source extension
does not promise that every embedded browser can decode the original directly.
"""

IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif',
    '.svg', '.ico', '.jfif', '.jpe', '.ppm', '.pgm', '.pbm', '.xbm',
    '.xpm', '.psd', '.exr', '.hdr',
})

VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mov', '.avi', '.mkv', '.webm', '.wmv', '.flv',
    '.m4v', '.3gp', '.ts',
})

GLB_EXTENSIONS = frozenset({'.glb', '.gltf'})

DROP_IMAGE_EXTENSIONS = IMAGE_EXTENSIONS | GLB_EXTENSIONS
