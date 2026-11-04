"""VTK adapter for Prism's shared bundled studio (no second lighting preset).

Babylon ENV stores PNG RGBD cube faces. Decode its original top mip into
linear HDR, preserving all six faces; VTK builds its own BRDF convolution.
The RGBD formula is the one in the bundled Babylon fromRGBD shader.
"""
import io
import json
from functools import lru_cache

import numpy as np
from PIL import Image
import vtk
from vtkmodules.util.numpy_support import numpy_to_vtk

from prism.material_preview_profile import ASSET_ROOT, preview_profile


@lru_cache(maxsize=1)
def environment_faces():
    profile = preview_profile('model')
    blob = (ASSET_ROOT / profile['environment']).read_bytes()
    if blob[:8] != b'\x86\x16\x87\x96\xf6\xd6\x96\x36':
        raise ValueError('Invalid bundled studio ENV signature')
    end = blob.index(b'\0', 8)
    header = json.loads(blob[8:end])
    width = header['width']
    if header['version'] != 1 or not 1 <= width <= 2048:
        raise ValueError('Unsupported bundled studio ENV')
    faces = []
    for entry in header['specular']['mipmaps'][:6]:
        start = end + 1 + entry['position']
        data = blob[start:start + entry['length']]
        if len(data) != entry['length']:
            raise ValueError('Truncated bundled studio face')
        with Image.open(io.BytesIO(data)) as image:
            if image.size != (width, width):
                raise ValueError('Invalid bundled studio face dimensions')
            rgbd = np.asarray(image.convert('RGBA'), dtype=np.float32) / 255
        if np.any(rgbd[:, :, 3] == 0):
            raise ValueError('Invalid bundled studio RGBD divisor')
        face = np.ascontiguousarray(np.flipud(
            rgbd[:, :, :3] ** profile['colorGamma'] / rgbd[:, :, 3:4]))
        face.setflags(write=False)
        faces.append(face)
    if len(faces) != 6:
        raise ValueError('Incomplete bundled studio cube')
    return tuple(faces)


def install_studio(renderer):
    profile = preview_profile('model')
    renderer.AutomaticLightCreationOff()
    for light in profile['lights']:
        source = vtk.vtkLight()
        source.SetLightTypeToSceneLight()
        source.SetPosition(*[-v for v in light['direction']])
        source.SetFocalPoint(0, 0, 0)
        source.SetIntensity(light['intensity'])
        renderer.AddLight(source)
    environment = vtk.vtkTexture()
    environment.CubeMapOn()
    environment.InterpolateOn()
    environment.MipmapOn()
    environment.SetColorModeToDirectScalars()
    for index, face in enumerate(environment_faces()):
        image = vtk.vtkImageData()
        image.SetDimensions(face.shape[1], face.shape[0], 1)
        pixels = face * profile['environmentIntensity']
        image.GetPointData().SetScalars(numpy_to_vtk(pixels.reshape(-1, 3), deep=True))
        environment.SetInputDataObject(index, image)
    renderer.SetEnvironmentTexture(environment, False)
    renderer.UseImageBasedLightingOn()
    # The bundled source is a cube, not an equirectangular panorama.
    renderer.UseSphericalHarmonicsOff()
    steps = vtk.vtkRenderStepsPass()
    tone = vtk.vtkToneMappingPass()
    tone.SetToneMappingType(vtk.vtkToneMappingPass.GenericFilmic)
    tone.SetUseACES(profile['toneMapping'] == 'aces')
    tone.SetExposure(profile['exposure'])
    tone.SetDelegatePass(steps)
    return tone
