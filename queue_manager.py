#!/usr/bin/env python3

import os
import sys
import time
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
import subprocess
import threading
from typing import List, Dict, Optional
from PIL import Image, ImageFont, ImageDraw
import glob

# Set up logging
logging.basicConfig(
    level=logging.INFO,  # Changed to DEBUG for more detailed logging
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('queue_manager.log')
    ]
)
logger = logging.getLogger(__name__)

class ComfyUIQueueManager:
    def __init__(self,
                 input_folder="/docker/SwarmUI/Input/",
                 output_folder="/docker/SwarmUI/OutputComfy/",
                 workflow_path="workflow.json",
                 server_address="localhost:7801",
                 supported_extensions=None):
        """
        Initialize ComfyUI Queue Manager

        Args:
            input_folder (str): Folder containing images to process
            output_folder (str): Folder to save processed images
            workflow_path (str): Path to ComfyUI workflow JSON
            server_address (str): SwarmUI server address
            supported_extensions (list): List of supported image extensions
        """
        self.input_folder = Path(input_folder)
        self.output_folder = Path(output_folder)
        self.workflow_path = workflow_path
        self.server_address = server_address
        self.supported_extensions = supported_extensions or ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp']

        # Queue management
        self.queue = []
        self.processed = []
        self.failed = []
        self.current_processing = None
        self.is_running = False
        self.pause_requested = False

        # Statistics
        self.stats = {
            'total_images': 0,
            'processed_count': 0,
            'failed_count': 0,
            'start_time': None,
            'current_image_start': None
        }

        # Ensure folders exist
        self.input_folder.mkdir(parents=True, exist_ok=True)
        self.output_folder.mkdir(parents=True, exist_ok=True)

        logger.info(f"Queue Manager initialized:")
        logger.info(f"  Input folder: {self.input_folder}")
        logger.info(f"  Output folder: {self.output_folder}")
        logger.info(f"  Workflow: {self.workflow_path}")
        logger.info(f"  Server: {self.server_address}")

    def scan_input_folder(self) -> List[Path]:
        """
        Scan input folder for supported image files

        Returns:
            List[Path]: List of image file paths
        """
        image_files = []

        if not self.input_folder.exists():
            logger.warning(f"Input folder does not exist: {self.input_folder}")
            return image_files

        for file_path in self.input_folder.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in self.supported_extensions:
                # Skip if already processed
                if not self.is_already_processed(file_path):
                    image_files.append(file_path)
                else:
                    logger.debug(f"Skipping already processed file: {file_path.name}")

        # Sort files for consistent processing order
        image_files.sort()
        logger.info(f"Found {len(image_files)} images to process")

        return image_files

    def is_already_processed(self, image_path: Path) -> bool:
        """
        Check if an image has already been processed

        Args:
            image_path (Path): Path to the image file

        Returns:
            bool: True if already processed
        """
        # Check if image is in processed list
        if str(image_path) in [str(p) for p in self.processed]:
            return True

        # Check if output file exists (basic check)
        # This assumes output files follow a pattern - you may want to customize this
        base_name = image_path.stem
        for output_file in self.output_folder.glob(f"*{base_name}*"):
            if output_file.is_file():
                return True

        return False

    def build_queue(self) -> int:
        """
        Build the processing queue from input folder

        Returns:
            int: Number of items in queue
        """
        self.queue = self.scan_input_folder()
        self.stats['total_images'] = len(self.queue)

        logger.info(f"Built queue with {len(self.queue)} images")
        for i, img_path in enumerate(self.queue[:5]):  # Log first 5
            logger.info(f"  {i+1}. {img_path.name}")

        if len(self.queue) > 5:
            logger.info(f"  ... and {len(self.queue) - 5} more images")

        return len(self.queue)

    def process_single_image(self, image_path: Path, create_comparison: bool = False, comparison_dir: Path = None) -> bool:
        """
        Process a single image through ComfyUI

        Args:
            image_path (Path): Path to the image to process
            create_comparison (bool): Whether to create comparison image after processing
            comparison_dir (Path): Directory to save comparison images

        Returns:
            bool: True if processing succeeded
        """
        try:
            logger.info(f"Processing: {image_path.name}")
            self.current_processing = image_path
            self.stats['current_image_start'] = datetime.now()

            # Build command to run main.py with increased timeout
            cmd = [
                sys.executable,  # Use same Python interpreter
                "main.py",
                str(image_path),
                "--output", str(self.output_folder),
                "--server", self.server_address,
                "--workflow", self.workflow_path
            ]

            # Log the command being executed
            logger.debug(f"Executing: {' '.join(cmd)}")

            # Run the command with increased timeout for model loading scenarios
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=900,  # 15 minutes timeout (increased from default)
                    cwd=Path(__file__).parent  # Run from script directory
                )
            except subprocess.TimeoutExpired:
                logger.error(f"❌ Processing timed out after 15 minutes: {image_path.name}")
                logger.error("This may indicate ComfyUI server issues or very slow model loading")
                self.failed.append({
                    'path': image_path,
                    'error': 'Process timeout (15 minutes)',
                    'exit_code': -1,
                    'timestamp': datetime.now()
                })
                self.stats['failed_count'] += 1
                return False

            # Process the result
            if result.returncode == 0:
                logger.info(f"✅ Successfully processed: {image_path.name}")
                self.processed.append(image_path)
                self.stats['processed_count'] += 1

                # Debug: log the actual output to see what we're parsing
                # Note: logging output typically goes to stderr, so check both
                full_output = result.stdout + "\n" + result.stderr
                logger.debug(f"Main.py full output:\n{full_output}")

                # Extract generated image info and create comparison if requested
                generated_output_path = None
                if "Processing complete!" in full_output:
                    logger.debug("Found 'Processing complete!' in output")
                    # Extract generated image info
                    lines = full_output.split('\n')
                    for line in lines:
                        if "- /docker/SwarmUI/OutputComfy/" in line:
                            output_file = line.strip().split("- ")[-1]
                            generated_output_path = Path(output_file)
                            logger.info(f"  Generated: {generated_output_path.name}")
                            break
                else:
                    logger.debug("'Processing complete!' not found in output")

                # Debug logging for comparison creation
                logger.debug(f"Comparison creation check: create_comparison={create_comparison}, generated_output_path={generated_output_path}")
                if generated_output_path:
                    logger.debug(f"Generated output path exists: {generated_output_path.exists()}")

                # Create comparison image immediately if requested and output was found
                if create_comparison and generated_output_path and generated_output_path.exists():
                    logger.info(f"Creating comparison for {image_path.name} -> {generated_output_path.name}")
                    comparison_path = self.create_comparison_image(image_path, generated_output_path, comparison_dir)
                    if comparison_path:
                        logger.info(f"  Comparison saved: {comparison_path.name}")
                    else:
                        logger.warning(f"  Failed to create comparison for {image_path.name}")
                elif create_comparison:
                    logger.warning(f"Cannot create comparison: output_path={generated_output_path}, exists={generated_output_path.exists() if generated_output_path else 'N/A'}")

                return True
            else:
                logger.error(f"❌ Failed to process: {image_path.name}")
                logger.error(f"Exit code: {result.returncode}")
                logger.error(f"Error output: {result.stderr}")

                self.failed.append({
                    'path': image_path,
                    'error': result.stderr,
                    'exit_code': result.returncode,
                    'timestamp': datetime.now()
                })
                self.stats['failed_count'] += 1
                return False

        except Exception as e:
            logger.error(f"❌ Exception processing {image_path.name}: {e}")
            self.failed.append({
                'path': image_path,
                'error': str(e),
                'exit_code': -1,
                'timestamp': datetime.now()
            })
            self.stats['failed_count'] += 1
            return False

        finally:
            self.current_processing = None

    def run_queue(self, max_images: Optional[int] = None, create_comparisons: bool = False, comparison_dir: Path = None) -> Dict:
        """
        Run the processing queue

        Args:
            max_images (int, optional): Maximum number of images to process
            create_comparisons (bool): Whether to create comparison images after each processing
            comparison_dir (Path): Directory to save comparison images

        Returns:
            Dict: Processing results summary
        """
        if not self.queue:
            logger.warning("No images in queue to process")
            return self.get_summary()

        self.is_running = True
        self.pause_requested = False
        self.stats['start_time'] = datetime.now()

        logger.info("🚀 Starting queue processing...")
        logger.info(f"Queue size: {len(self.queue)} images")
        if create_comparisons:
            logger.info("📸 Comparison images will be created after each processing")

        if max_images:
            process_count = min(max_images, len(self.queue))
            logger.info(f"Processing limit: {max_images} images")
        else:
            process_count = len(self.queue)

        try:
            for i, image_path in enumerate(self.queue[:process_count]):
                if self.pause_requested:
                    logger.info("⏸️  Processing paused by request")
                    break

                progress = f"[{i+1}/{process_count}]"
                logger.info(f"{progress} Starting: {image_path.name}")

                # Process the image with comparison creation if requested
                success = self.process_single_image(image_path, create_comparisons, comparison_dir)

                # Calculate and log progress
                elapsed = datetime.now() - self.stats['start_time']
                avg_time = elapsed.total_seconds() / (i + 1)
                remaining = (process_count - i - 1) * avg_time

                logger.info(f"{progress} {'✅ Completed' if success else '❌ Failed'}: {image_path.name}")
                logger.info(f"Progress: {self.stats['processed_count']}/{process_count} processed, "
                          f"{self.stats['failed_count']} failed")
                logger.info(f"Time: {elapsed.seconds//60}m {elapsed.seconds%60}s elapsed, "
                          f"~{remaining//60:.0f}m {remaining%60:.0f}s remaining")

                # Small delay between images to prevent overwhelming the server
                if i < process_count - 1:  # Don't delay after the last image
                    logger.debug("Waiting 2 seconds before next image...")
                    time.sleep(2)

        except KeyboardInterrupt:
            logger.info("⏹️  Processing interrupted by user")

        finally:
            self.is_running = False

        # Final summary
        summary = self.get_summary()
        self.log_summary(summary)

        return summary

    def get_summary(self) -> Dict:
        """Get processing summary"""
        elapsed = None
        if self.stats['start_time']:
            elapsed = datetime.now() - self.stats['start_time']

        return {
            'total_images': self.stats['total_images'],
            'processed_count': self.stats['processed_count'],
            'failed_count': self.stats['failed_count'],
            'remaining': len(self.queue) - self.stats['processed_count'] - self.stats['failed_count'],
            'elapsed_time': elapsed,
            'is_running': self.is_running,
            'current_processing': str(self.current_processing) if self.current_processing else None,
            'processed_files': [str(p) for p in self.processed[-5:]],  # Last 5 processed
            'failed_files': [f['path'].name for f in self.failed[-5:]]  # Last 5 failed
        }

    def log_summary(self, summary: Dict):
        """Log processing summary"""
        logger.info("📊 Processing Summary:")
        logger.info(f"  Total images: {summary['total_images']}")
        logger.info(f"  Successfully processed: {summary['processed_count']}")
        logger.info(f"  Failed: {summary['failed_count']}")
        logger.info(f"  Remaining: {summary['remaining']}")

        if summary['elapsed_time']:
            elapsed = summary['elapsed_time']
            logger.info(f"  Total time: {elapsed.seconds//3600}h {(elapsed.seconds//60)%60}m {elapsed.seconds%60}s")

        if summary['failed_count'] > 0:
            logger.warning("Failed images:")
            for failed_file in summary['failed_files']:
                logger.warning(f"  - {failed_file}")

    def pause(self):
        """Request to pause processing after current image"""
        self.pause_requested = True
        logger.info("⏸️  Pause requested - will stop after current image completes")

    def status(self) -> Dict:
        """Get current status"""
        return self.get_summary()

    def find_output_files(self, input_path: Path) -> List[Path]:
        """
        Find output files that correspond to an input file

        Args:
            input_path (Path): Path to the input image

        Returns:
            List[Path]: List of corresponding output files
        """
        output_files = []
        input_stem = input_path.stem

        # Look for files that might contain the input filename or similar patterns
        patterns = [
            f"*{input_stem}*",  # Files containing the input filename
            f"Sat13_*",  # Your workflow's output pattern
            f"ComfyUI_*"  # General ComfyUI output pattern
        ]

        for pattern in patterns:
            matches = list(self.output_folder.glob(pattern))
            for match in matches:
                if match.is_file() and match.suffix.lower() in self.supported_extensions:
                    # Check if this output was created around the time we processed the input
                    if match not in output_files:  # Avoid duplicates
                        output_files.append(match)

        # Sort by modification time (most recent first)
        output_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return output_files

    def create_comparison_image(self, input_path: Path, output_path: Path, comparison_dir: Path = None) -> Optional[Path]:
        """
        Create a side-by-side comparison image

        Args:
            input_path (Path): Path to the input image
            output_path (Path): Path to the output image
            comparison_dir (Path): Directory to save comparison images

        Returns:
            Optional[Path]: Path to the created comparison image, or None if failed
        """
        if comparison_dir is None:
            comparison_dir = self.output_folder / "comparisons"

        comparison_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Open both images
            input_img = Image.open(input_path)
            output_img = Image.open(output_path)

            # Convert to RGB if necessary (for JPEG compatibility)
            if input_img.mode != 'RGB':
                input_img = input_img.convert('RGB')
            if output_img.mode != 'RGB':
                output_img = output_img.convert('RGB')

            # Calculate dimensions for side-by-side layout
            max_height = max(input_img.height, output_img.height)

            # Resize images to same height while maintaining aspect ratio
            if input_img.height != max_height:
                ratio = max_height / input_img.height
                new_width = int(input_img.width * ratio)
                input_img = input_img.resize((new_width, max_height), Image.Resampling.LANCZOS)

            if output_img.height != max_height:
                ratio = max_height / output_img.height
                new_width = int(output_img.width * ratio)
                output_img = output_img.resize((new_width, max_height), Image.Resampling.LANCZOS)

            # Create comparison image
            gap_width = 20  # Gap between images
            total_width = input_img.width + output_img.width + gap_width
            total_height = max_height + 60  # Extra space for labels

            # Create new image with white background
            comparison_img = Image.new('RGB', (total_width, total_height), 'white')

            # Paste input image on the left
            comparison_img.paste(input_img, (0, 30))  # 30px from top for label

            # Paste output image on the right
            comparison_img.paste(output_img, (input_img.width + gap_width, 30))

            # Add labels
            try:
                # Try to use a decent font
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
            except:
                # Fall back to default font
                font = ImageFont.load_default()

            draw = ImageDraw.Draw(comparison_img)

            # Add "INPUT" label
            input_label = "INPUT"
            input_bbox = draw.textbbox((0, 0), input_label, font=font)
            input_label_width = input_bbox[2] - input_bbox[0]
            input_x = (input_img.width - input_label_width) // 2
            draw.text((input_x, 5), input_label, fill='black', font=font)

            # Add "OUTPUT" label
            output_label = "OUTPUT"
            output_bbox = draw.textbbox((0, 0), output_label, font=font)
            output_label_width = output_bbox[2] - output_bbox[0]
            output_x = input_img.width + gap_width + (output_img.width - output_label_width) // 2
            draw.text((output_x, 5), output_label, fill='black', font=font)

            # Add filenames at bottom
            input_filename = input_path.name
            output_filename = output_path.name

            # Input filename
            if len(input_filename) > 30:
                input_filename = input_filename[:27] + "..."
            input_name_bbox = draw.textbbox((0, 0), input_filename, font=font)
            input_name_width = input_name_bbox[2] - input_name_bbox[0]
            input_name_x = (input_img.width - input_name_width) // 2
            draw.text((input_name_x, total_height - 25), input_filename, fill='gray', font=font)

            # Output filename
            if len(output_filename) > 30:
                output_filename = output_filename[:27] + "..."
            output_name_bbox = draw.textbbox((0, 0), output_filename, font=font)
            output_name_width = output_name_bbox[2] - output_name_bbox[0]
            output_name_x = input_img.width + gap_width + (output_img.width - output_name_width) // 2
            draw.text((output_name_x, total_height - 25), output_filename, fill='gray', font=font)

            # Save comparison image
            comparison_filename = f"comparison_{input_path.stem}_{output_path.stem}.jpg"
            comparison_path = comparison_dir / comparison_filename
            comparison_img.save(comparison_path, "JPEG", quality=95)

            logger.info(f"Created comparison: {comparison_path.name}")
            return comparison_path

        except Exception as e:
            logger.error(f"Failed to create comparison for {input_path.name}: {e}")
            return None

    def create_all_comparisons(self, comparison_dir: Path = None) -> int:
        """
        Create comparison images for all processed files

        Args:
            comparison_dir (Path): Directory to save comparison images

        Returns:
            int: Number of comparison images created
        """
        if comparison_dir is None:
            comparison_dir = self.output_folder / "comparisons"

        logger.info("Creating comparison images for processed files...")
        comparison_count = 0

        for input_path in self.processed:
            # Find corresponding output files
            output_files = self.find_output_files(input_path)

            if output_files:
                # Use the most recent output file
                output_path = output_files[0]
                comparison_path = self.create_comparison_image(input_path, output_path, comparison_dir)
                if comparison_path:
                    comparison_count += 1
            else:
                logger.warning(f"No output file found for {input_path.name}")

        logger.info(f"Created {comparison_count} comparison images in {comparison_dir}")
        return comparison_count


def main():
    parser = argparse.ArgumentParser(description='ComfyUI Queue Manager - Process images sequentially')
    parser.add_argument('--input', default='/docker/SwarmUI/Input/', help='Input folder containing images')
    parser.add_argument('--output', default='/docker/SwarmUI/OutputComfy/', help='Output folder for processed images')
    parser.add_argument('--workflow', default='workflow.json', help='Path to ComfyUI workflow JSON file')
    parser.add_argument('--server', default='localhost:7801', help='SwarmUI server address:port')
    parser.add_argument('--max-images', type=int, help='Maximum number of images to process')
    parser.add_argument('--scan-only', action='store_true', help='Only scan and show queue, don\'t process')
    parser.add_argument('--create-comparisons', action='store_true', help='Create side-by-side comparison images after processing')
    parser.add_argument('--comparisons-only', action='store_true', help='Only create comparison images, don\'t process queue')
    parser.add_argument('--comparison-dir', help='Directory to save comparison images (default: output/comparisons)')
    parser.add_argument('--extensions', nargs='+', default=['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'],
                        help='Supported image file extensions')

    args = parser.parse_args()

    # Create queue manager
    manager = ComfyUIQueueManager(
        input_folder=args.input,
        output_folder=args.output,
        workflow_path=args.workflow,
        server_address=args.server,
        supported_extensions=args.extensions
    )

    # Build the queue
    queue_size = manager.build_queue()

    if queue_size == 0:
        logger.info("No images found to process.")
        return

    if args.scan_only:
        logger.info("Scan complete. Use without --scan-only to start processing.")
        return

    # Handle comparisons-only mode
    if args.comparisons_only:
        logger.info("Creating comparison images for existing processed files...")
        comparison_dir = Path(args.comparison_dir) if args.comparison_dir else None

        # Load previously processed files from all images that have outputs
        all_input_files = manager.scan_input_folder()
        processed_files = []

        for input_file in all_input_files:
            if manager.is_already_processed(input_file):
                processed_files.append(input_file)

        if not processed_files:
            logger.warning("No processed files found to create comparisons for.")
            return

        manager.processed = processed_files  # Set processed list
        comparison_count = manager.create_all_comparisons(comparison_dir)

        if comparison_count > 0:
            logger.info(f"Successfully created {comparison_count} comparison images!")
        else:
            logger.warning("No comparison images were created.")
        return

    # Start processing
    try:
        # Set up comparison directory if needed
        comparison_dir = Path(args.comparison_dir) if args.comparison_dir else None

        # Run the queue with comparison creation built-in
        summary = manager.run_queue(
            max_images=args.max_images,
            create_comparisons=args.create_comparisons,
            comparison_dir=comparison_dir
        )

        # Exit with appropriate code
        if summary['failed_count'] > 0:
            sys.exit(1)  # Some failures occurred
        else:
            sys.exit(0)  # All successful

    except KeyboardInterrupt:
        logger.info("Processing interrupted by user")
        sys.exit(130)  # Standard exit code for Ctrl+C


if __name__ == "__main__":
    main()
