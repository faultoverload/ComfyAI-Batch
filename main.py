#!/usr/bin/env python3

import json
import urllib.request
import urllib.parse
import uuid
import os
import sys
import argparse
import time
import websocket
import threading
from PIL import Image
import io
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ComfyUIBatch:
    def __init__(self, server_address="localhost:7801", workflow_path="workflow.json", use_swarm=True):
        """
        Initialize ComfyUI Batch processor

        Args:
            server_address (str): Server address and port (SwarmUI: localhost:7801, Direct ComfyUI: localhost:8188)
            workflow_path (str): Path to the workflow JSON file
            use_swarm (bool): Whether to use SwarmUI API (True) or direct ComfyUI (False)
        """
        self.server_address = server_address
        self.workflow_path = workflow_path
        self.use_swarm = use_swarm
        self.client_id = str(uuid.uuid4())
        self.workflow = None
        self.ws = None
        self.current_prompt_id = None
        self.progress_callback = None
        self.session_id = None

        # Load workflow on initialization
        self.load_workflow()

        # Get SwarmUI session if needed
        if self.use_swarm:
            self.get_swarm_session()

    def get_swarm_session(self):
        """Get SwarmUI session ID"""
        try:
            url = f"http://{self.server_address}/API/GetNewSession"
            req = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'})

            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode())
                self.session_id = result.get('session_id')
                logger.info(f"Got SwarmUI session: {self.session_id}")
                return result

        except Exception as e:
            logger.error(f"Failed to get SwarmUI session: {e}")
            return None

    def load_workflow(self):
        """Load workflow from JSON file"""
        try:
            with open(self.workflow_path, 'r') as f:
                self.workflow = json.load(f)
            logger.info(f"Loaded workflow from {self.workflow_path}")
        except FileNotFoundError:
            logger.error(f"Workflow file not found: {self.workflow_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in workflow file: {e}")
            sys.exit(1)

    def upload_image(self, image_path):
        """
        Upload an image to ComfyUI server (via SwarmUI or direct)

        Args:
            image_path (str): Path to the image file

        Returns:
            dict: Response containing uploaded image info
        """
        if not os.path.exists(image_path):
            logger.error(f"Image file not found: {image_path}")
            return None

        try:
            with open(image_path, 'rb') as f:
                # Prepare multipart form data
                boundary = f'----WebKitFormBoundary{uuid.uuid4().hex}'

                # Create multipart body
                body_parts = []

                # Add image file
                filename = os.path.basename(image_path)
                body_parts.append(f'--{boundary}'.encode())
                body_parts.append(f'Content-Disposition: form-data; name="image"; filename="{filename}"'.encode())
                body_parts.append(b'Content-Type: image/png')
                body_parts.append(b'')
                body_parts.append(f.read())

                # Add overwrite parameter
                body_parts.append(f'--{boundary}'.encode())
                body_parts.append(b'Content-Disposition: form-data; name="overwrite"')
                body_parts.append(b'')
                body_parts.append(b'false')

                # Close boundary
                body_parts.append(f'--{boundary}--'.encode())

                body = b'\r\n'.join(body_parts)

                # Create request - different URL for SwarmUI vs direct ComfyUI
                if self.use_swarm:
                    url = f"http://{self.server_address}/ComfyBackendDirect/upload/image"
                else:
                    url = f"http://{self.server_address}/upload/image"

                req = urllib.request.Request(url, data=body)
                req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')

                # Send request
                with urllib.request.urlopen(req) as response:
                    result = json.loads(response.read().decode())
                    logger.info(f"Successfully uploaded image: {filename}")
                    return result

        except Exception as e:
            logger.error(f"Failed to upload image {image_path}: {e}")
            return None

    def queue_prompt(self, prompt_data):
        """
        Queue a prompt for processing (via SwarmUI or direct ComfyUI)

        Args:
            prompt_data (dict): The workflow prompt data

        Returns:
            dict: Response containing prompt ID
        """
        try:
            if self.use_swarm:
                # Use SwarmUI's ComfyUI direct backend
                p = {
                    "prompt": prompt_data,
                    "client_id": self.client_id
                }
                url = f"http://{self.server_address}/ComfyBackendDirect/prompt"
            else:
                # Direct ComfyUI
                p = {
                    "prompt": prompt_data,
                    "client_id": self.client_id
                }
                url = f"http://{self.server_address}/prompt"

            # If the workflow contains API nodes, you can add a Comfy API key
            # p["extra_data"] = {
            #     "api_key_comfy_org": "your-api-key-here"
            # }

            data = json.dumps(p).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})

            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode())
                self.current_prompt_id = result.get('prompt_id')
                logger.info(f"Queued prompt with ID: {self.current_prompt_id}")
                return result

        except Exception as e:
            logger.error(f"Failed to queue prompt: {e}")
            return None

    def setup_websocket(self):
        """Setup WebSocket connection for progress tracking"""
        try:
            if self.use_swarm:
                ws_url = f"ws://{self.server_address}/ComfyBackendDirect/ws?clientId={self.client_id}"
            else:
                ws_url = f"ws://{self.server_address}/ws?clientId={self.client_id}"

            self.ws = websocket.WebSocketApp(
                ws_url,
                on_message=self.on_websocket_message,
                on_error=self.on_websocket_error,
                on_close=self.on_websocket_close,
                on_open=self.on_websocket_open
            )
            logger.info("WebSocket connection established")
        except Exception as e:
            logger.error(f"Failed to setup WebSocket: {e}")

    def on_websocket_open(self, ws):
        """WebSocket opened callback"""
        logger.info("WebSocket connection opened")

    def on_websocket_message(self, ws, message):
        """Handle WebSocket messages for progress tracking"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')

            if msg_type == 'status':
                # Handle status updates
                status_data = data.get('data', {})
                exec_info = status_data.get('status', {}).get('exec_info', {})
                queue_remaining = exec_info.get('queue_remaining', 0)

                if queue_remaining > 0:
                    logger.info(f"Queue position: {queue_remaining}")

            elif msg_type == 'progress':
                # Handle progress updates
                progress_data = data.get('data', {})
                value = progress_data.get('value', 0)
                max_val = progress_data.get('max', 100)
                node = progress_data.get('node')

                progress_percent = (value / max_val * 100) if max_val > 0 else 0
                logger.info(f"Progress: {progress_percent:.1f}% (Node: {node})")

                if self.progress_callback:
                    self.progress_callback(progress_percent, node)

            elif msg_type == 'executing':
                # Handle execution status
                executing_data = data.get('data', {})
                node = executing_data.get('node')
                prompt_id = executing_data.get('prompt_id')

                if node is None and prompt_id == self.current_prompt_id:
                    logger.info("Execution completed!")
                elif node:
                    logger.info(f"Executing node: {node}")

        except json.JSONDecodeError:
            logger.warning(f"Received non-JSON WebSocket message: {message}")
        except Exception as e:
            logger.error(f"Error processing WebSocket message: {e}")

    def on_websocket_error(self, ws, error):
        """WebSocket error callback"""
        logger.error(f"WebSocket error: {error}")

    def on_websocket_close(self, ws, close_status_code, close_msg):
        """WebSocket closed callback"""
        logger.info("WebSocket connection closed")

    def get_history(self, prompt_id):
        """
        Get the execution history for a prompt

        Args:
            prompt_id (str): The prompt ID to get history for

        Returns:
            dict: History data
        """
        try:
            if self.use_swarm:
                url = f"http://{self.server_address}/ComfyBackendDirect/history/{prompt_id}"
            else:
                url = f"http://{self.server_address}/history/{prompt_id}"

            with urllib.request.urlopen(url) as response:
                return json.loads(response.read().decode())
        except Exception as e:
            logger.error(f"Failed to get history: {e}")
            return None

    def get_images(self, prompt_id, save_dir="output"):
        """
        Download generated images

        Args:
            prompt_id (str): The prompt ID
            save_dir (str): Directory to save images

        Returns:
            list: List of saved image paths
        """
        history = self.get_history(prompt_id)
        if not history:
            return []

        # Create output directory if it doesn't exist
        os.makedirs(save_dir, exist_ok=True)

        saved_images = []

        try:
            prompt_data = history[prompt_id]
            outputs = prompt_data.get('outputs', {})

            for node_id, node_output in outputs.items():
                if 'images' in node_output:
                    for image_data in node_output['images']:
                        filename = image_data['filename']
                        subfolder = image_data.get('subfolder', '')

                        # Construct the URL to download the image
                        if self.use_swarm:
                            if subfolder:
                                image_url = f"http://{self.server_address}/ComfyBackendDirect/view?filename={filename}&subfolder={subfolder}&type=output"
                            else:
                                image_url = f"http://{self.server_address}/ComfyBackendDirect/view?filename={filename}&type=output"
                        else:
                            if subfolder:
                                image_url = f"http://{self.server_address}/view?filename={filename}&subfolder={subfolder}&type=output"
                            else:
                                image_url = f"http://{self.server_address}/view?filename={filename}&type=output"

                        # Download and save the image
                        try:
                            with urllib.request.urlopen(image_url) as response:
                                image_data_bytes = response.read()

                            # Save the image
                            output_path = os.path.join(save_dir, filename)
                            with open(output_path, 'wb') as f:
                                f.write(image_data_bytes)

                            saved_images.append(output_path)
                            logger.info(f"Saved image: {output_path}")

                        except Exception as e:
                            logger.error(f"Failed to download image {filename}: {e}")

        except Exception as e:
            logger.error(f"Failed to process images: {e}")

        return saved_images

    def process_image(self, image_path, output_dir="output", wait_for_completion=True, progress_callback=None):
        """
        Process a single image through the ComfyUI workflow

        Args:
            image_path (str): Path to the input image
            output_dir (str): Directory to save output images
            wait_for_completion (bool): Whether to wait for completion
            progress_callback (function): Callback for progress updates

        Returns:
            list: List of generated image paths
        """
        self.progress_callback = progress_callback

        # Upload the image first
        upload_result = self.upload_image(image_path)
        if not upload_result:
            logger.error("Failed to upload image")
            return []

        # Update workflow to use the uploaded image
        # This finds and updates image input nodes based on your workflow structure
        workflow_copy = self.workflow.copy()

        # Find and update image input nodes - handle different node types
        image_updated = False
        for node_id, node in workflow_copy.items():
            class_type = node.get('class_type', '')

            # Standard LoadImage node
            if class_type == 'LoadImage':
                node['inputs']['image'] = upload_result['name']
                logger.info(f"Updated LoadImage node {node_id} with image: {upload_result['name']}")
                image_updated = True
                break

            # vsLinx LoadSelectedImagesBatch node (your workflow uses this)
            elif class_type == 'vsLinx_LoadSelectedImagesBatch':
                # Update the selected_paths to include the uploaded image
                node['inputs']['selected_paths'] = f'["{upload_result["name"]}"]'
                logger.info(f"Updated vsLinx_LoadSelectedImagesBatch node {node_id} with image: {upload_result['name']}")
                image_updated = True
                break

            # Other possible image input node types
            elif 'image' in node.get('inputs', {}) and isinstance(node['inputs']['image'], str):
                # Direct image filename input
                node['inputs']['image'] = upload_result['name']
                logger.info(f"Updated {class_type} node {node_id} with image: {upload_result['name']}")
                image_updated = True
                break

        if not image_updated:
            logger.warning("Could not find image input node in workflow - image may not be properly set")
            logger.info("Available node types in workflow:")
            for node_id, node in workflow_copy.items():
                logger.info(f"  Node {node_id}: {node.get('class_type', 'Unknown')}")

        # Setup WebSocket for progress tracking
        if wait_for_completion:
            self.setup_websocket()
            ws_thread = threading.Thread(target=self.ws.run_forever)
            ws_thread.daemon = True
            ws_thread.start()

        # Queue the prompt
        queue_result = self.queue_prompt(workflow_copy)
        if not queue_result:
            logger.error("Failed to queue prompt")
            return []

        prompt_id = queue_result.get('prompt_id')

        if wait_for_completion:
            # Wait for completion
            logger.info("Waiting for processing to complete...")
            while True:
                history = self.get_history(prompt_id)
                if history and prompt_id in history:
                    # Check if execution is complete
                    prompt_data = history[prompt_id]
                    if prompt_data.get('status', {}).get('status_str') == 'success':
                        break
                    elif prompt_data.get('status', {}).get('status_str') == 'error':
                        logger.error("Processing failed!")
                        return []

                time.sleep(1)

            # Close WebSocket
            if self.ws:
                self.ws.close()

            # Download generated images
            return self.get_images(prompt_id, output_dir)

        return [prompt_id]  # Return prompt_id for manual checking


def main():
    parser = argparse.ArgumentParser(description='ComfyUI Batch Image Processor')
    parser.add_argument('image', help='Path to input image')
    parser.add_argument('--server', default='localhost:7801', help='Server address:port (SwarmUI: localhost:7801, Direct ComfyUI: localhost:8188)')
    parser.add_argument('--workflow', default='workflow.json', help='Path to workflow JSON file')
    parser.add_argument('--output', default='output', help='Output directory for generated images')
    parser.add_argument('--no-wait', action='store_true', help='Don\'t wait for completion')
    parser.add_argument('--direct-comfy', action='store_true', help='Use direct ComfyUI instead of SwarmUI')

    args = parser.parse_args()

    # Create ComfyUI processor - SwarmUI by default, unless --direct-comfy is specified
    use_swarm = not args.direct_comfy
    processor = ComfyUIBatch(
        server_address=args.server,
        workflow_path=args.workflow,
        use_swarm=use_swarm
    )

    # Progress callback
    def progress_callback(percent, node):
        print(f"Progress: {percent:.1f}% - Node: {node}")

    # Process the image
    logger.info(f"Processing image: {args.image}")
    logger.info(f"Using {'SwarmUI' if use_swarm else 'Direct ComfyUI'} API")
    result = processor.process_image(
        image_path=args.image,
        output_dir=args.output,
        wait_for_completion=not args.no_wait,
        progress_callback=progress_callback
    )

    if args.no_wait:
        logger.info(f"Prompt queued with ID: {result[0]}")
    else:
        if result:
            logger.info(f"Processing complete! Generated {len(result)} images:")
            for img_path in result:
                logger.info(f"  - {img_path}")
        else:
            logger.error("Processing failed!")
if __name__ == "__main__":
    main()


