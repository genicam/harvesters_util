#!/usr/bin/env python3
# ----------------------------------------------------------------------------
#
# Copyright 2018 EMVA
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ----------------------------------------------------------------------------


# Standard library imports

# Related third party imports

import numpy as np

from vispy import gloo
from vispy import app
from vispy.gloo import Program
from vispy.util.transforms import ortho

from genicam2.gentl import PAYLOADTYPE_INFO_IDS

# Local application/library specific imports
from harvesters._private.core.helper.system import is_running_on_macos
from harvesters.pfnc import is_custom, get_bits_per_pixel, \
    component_bgr_formats


class CanvasBase(app.Canvas):
    def __init__(
            self, *,
            image_acquisition_manager=None,
            width=640, height=480,
            fps=30.,
            background_color='gray'
    ):
        """
        As far as we know, Vispy refreshes the canvas every 1/30 sec at the
        fastest no matter which faster number is specified. If we set any
        value which is greater than 30, then Vispy's callback is randomly
        called.
        """

        #
        app.Canvas.__init__(
            self, size=(width, height), vsync=True, autoswap=True
        )

        #
        self._iam = image_acquisition_manager

        #
        self._background_color = background_color
        self._has_filled_texture = False
        self._width, self._height = width, height

        #
        self._is_dragging = False

        # If it's True , the canvas keeps image acquisition but do not
        # draw images on the canvas.
        self._pause_drawing = False

        #
        self._origin = [0, 0]

        #
        self._timer = app.Timer(1./fps, connect=self.update, start=True)

        #
        self._buffers = []

    def set_canvas_size(self, width, height):
        #
        self._has_filled_texture = False

        #
        updated = False

        #
        if self._width != width or self._height != height:
            self._width = width
            self._height = height
            updated = True

        #
        if updated:
            self.apply_magnification()

    def on_draw(self, event):
        # Clear the canvas in gray.
        gloo.clear(color=self._background_color)

        drew = False
        try:
            if not self._pause_drawing:
                # Fetch a buffer.
                buffer = self.iam.fetch_buffer(timeout_s=0.0001)

                if buffer:
                    # Prepare a texture to draw:
                    self._prepare_texture(buffer)

                    # Draw the texture until the buffer object exists
                    # within this scope:
                    self._draw()

                    #
                    self.release_buffers()

                    # We have drawn the latest image on the canvas:
                    drew = True

                    #
                    self._buffers.append(buffer)

        except AttributeError:
            # Harvester Core has not started image acquisition so
            # calling fetch_buffer() raises AttributeError because
            # None object is used for the with statement.

            # Update on June 15th, 2018:
            # According to a VisPy developer, they have not finished
            # porting VisPy to PyQt5. Once they finished the development
            # we should try it out if it gives us the maximum refresh rate.
            # See the following URL to check the latest information:
            #
            #     https://github.com/vispy/vispy/issues/1394
            pass

        # Draw the latest texture again if needed:
        if not drew:
            self._draw()

    def release_buffers(self):
        for _buffer in self._buffers:
            if _buffer:
                _buffer.queue()
        self._buffers.clear()

    def _draw(self):
        raise NotImplementedError

    def on_resize(self, event):
        self.apply_magnification()

    def apply_magnification(self):
        raise NotImplementedError

    def on_mouse_wheel(self, event):
        raise NotImplementedError

    def on_mouse_press(self, event):
        self._is_dragging = True
        self._origin = event.pos

    def on_mouse_release(self, event):
        self._is_dragging = False

    def on_mouse_move(self, event):
        raise NotImplementedError

    def pause_drawing(self, pause=True):
        self._pause_drawing = pause

    def toggle_drawing(self):
        self._pause_drawing = False if self._pause_drawing else True

    @property
    def is_pausing(self):
        return True if self._pause_drawing else False

    def resume_drawing(self):
        self._pause_drawing = False

    @property
    def background_color(self):
        return self._background_color

    @background_color.setter
    def background_color(self, color):
        self._background_color = color

    @property
    def iam(self):
        return self._iam

    @iam.setter
    def iam(self, value):
        self._iam = value
        # Register a method which is called at stop_image_acquisition:
        self._iam.tear_down = self.release_buffers

    def _prepare_texture(self, buffer):
        raise NotImplementedError


class Canvas2D(CanvasBase):
    _visible_payloads = [
        PAYLOADTYPE_INFO_IDS.PAYLOAD_TYPE_IMAGE,
        PAYLOADTYPE_INFO_IDS.PAYLOAD_TYPE_CHUNK_DATA,
        PAYLOADTYPE_INFO_IDS.PAYLOAD_TYPE_MULTI_PART,
    ]

    def __init__(
            self, *,
            image_acquisition_manager=None,
            width=640, height=480,
            background_color='gray'
    ):
        #
        super().__init__(
            image_acquisition_manager=image_acquisition_manager,
            width=width, height=height,
            fps=30.,
            background_color=background_color
        )

        #
        self._vertex_shader = """
            // Uniforms
            uniform mat4 u_model;
            uniform mat4 u_view;
            uniform mat4 u_projection;

            // Attributes
            attribute vec2 a_position;
            attribute vec2 a_texcoord;

            // Varyings
            varying vec2 v_texcoord;

            // Main
            void main (void)
            {
                v_texcoord = a_texcoord;
                gl_Position = u_projection * u_view * u_model * vec4(a_position, 0.0, 1.0);
            }
        """

        self._fragment_shader = """
            varying vec2 v_texcoord;
            uniform sampler2D texture;
            void main()
            {
                gl_FragColor = texture2D(texture, v_texcoord);
            }
        """

        #
        self._program = None
        self._data = None
        self._coordinate = None
        self._translate = 0.
        self._latest_translate = self._translate
        self._magnification = 1.

        # Apply shaders.
        self._program = Program(
            self._vertex_shader, self._fragment_shader, count=4
        )

        #
        self._data = np.zeros(
            4, dtype=[
                ('a_position', np.float32, 2),
                ('a_texcoord', np.float32, 2)
            ]
        )

        #
        self._data['a_texcoord'] = np.array(
            [[0., 1.], [1., 1.], [0., 0.], [1., 0.]]
        )

        #
        self._program['u_model'] = np.eye(4, dtype=np.float32)
        self._program['u_view'] = np.eye(4, dtype=np.float32)

        #
        self._coordinate = [0, 0]


        #
        self._program['texture'] = np.zeros(
            (self._height, self._width), dtype='uint8'
        )

        #
        self.apply_magnification()

    def _prepare_texture(self, buffer):
        update = True
        if buffer.payload_type not in self._visible_payloads:
            update = False

        # Set the image as the texture of our canvas.
        if buffer:
            # Update the canvas size if needed.
            self.set_canvas_size(
                buffer.payload.components[0].width,
                buffer.payload.components[0].height
            )

            #
            exponent = 0
            data_format = None

            #
            data_format_value = buffer.payload.components[0].data_format_value
            if is_custom(data_format_value):
                update = False
            else:
                data_format = buffer.payload.components[0].data_format
                bpp = get_bits_per_pixel(data_format)
                if bpp is not None:
                    exponent = bpp - 8
                else:
                    update = False

            if update:
                # Convert each data to an 8bit.
                content = buffer.payload.components[0].data
                if exponent > 0:
                    # The following code may affect to the rendering
                    # performance:
                    content = (content / (2 ** exponent))

                    # Then cast each array element to an uint8:
                    content = content.astype(np.uint8)

                if data_format in component_bgr_formats:
                    # Swap every R and B:
                    content = content[:, :, ::-1]

                self._program['texture'] = content

    def _draw(self):
        self._program.draw('triangle_strip')

    def apply_magnification(self):
        #
        canvas_w, canvas_h = self.physical_size
        gloo.set_viewport(0, 0, canvas_w, canvas_h)

        #
        ratio = self._magnification
        w, h = self._width, self._height

        self._program['u_projection'] = ortho(
            self._coordinate[0],
            canvas_w * ratio + self._coordinate[0],
            self._coordinate[1],
            canvas_h * ratio + self._coordinate[1],
            -1, 1
        )

        x, y = int((canvas_w * ratio - w) / 2), int((canvas_h * ratio - h) / 2)  # centering x & y

        #
        self._data['a_position'] = np.array(
            [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]
        )

        #
        self._program.bind(gloo.VertexBuffer(self._data))

    def on_mouse_wheel(self, event):
        self._translate += event.delta[1]
        power = 7. if is_running_on_macos() else 5.  # 2 ** exponent
        stride = 4. if is_running_on_macos() else 7.
        translate = self._translate
        translate = min(power * stride, translate)
        translate = max(-power * stride, translate)
        self._translate = translate
        self._magnification = 2 ** -(self._translate / stride)
        if self._latest_translate != self._translate:
            self.apply_magnification()
            self._latest_translate = self._translate

    def on_mouse_move(self, event):
        if self._is_dragging:
            adjustment = 2. if is_running_on_macos() else 1.
            ratio = self._magnification * adjustment
            delta = event.pos - self._origin
            self._origin = event.pos
            self._coordinate[0] -= (delta[0] * ratio)
            self._coordinate[1] += (delta[1] * ratio)
            self.apply_magnification()
