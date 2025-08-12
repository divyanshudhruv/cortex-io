from textwrap import dedent
# --- Tool: about ---

import asyncio
import os
import io
import base64
from typing import Annotated, Dict, Optional
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import INTERNAL_ERROR

from PIL import Image, ImageOps, ImageFilter # Import necessary PIL modules
import img2pdf # For simple image-to-PDF
import numpy as np # Used for basic array operations, primarily with PIL image data

# --- Load environment variables ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
assert TOKEN, "AUTH_TOKEN environment variable not set!"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    """
    A simple bearer token authentication provider for FastMCP.
    Uses a pre-defined token for authentication.
    """
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        """
        Loads an access token if the provided token matches the internal token.
        """
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="receipt-processor-client",
                scopes=["*"], # All scopes are allowed for this client
                expires_at=None, # Token does not expire
            )
        return None # Return None if token is invalid

# --- Receipt Processing Manager (Pillow-Based) ---
class ReceiptProcessor:
    def _preprocess_image(self, pil_image: Image.Image) -> Image.Image:
        """Applies basic preprocessing for receipt images using Pillow."""
        # Convert to grayscale
        gray_image = pil_image.convert("L")
        # Apply a slight blur to reduce noise
        blurred_image = gray_image.filter(ImageFilter.GaussianBlur(radius=2))
        
     # threshold
        
        # Enhance contrast to make text stand out before thresholding
        enhanced_image = ImageOps.autocontrast(blurred_image)

        # Basic global thresholding (adjust threshold value as needed for your receipts)
        # You might need to experiment with this value (e.g., 128, 150, 180)
        threshold_value = 150 
        binary_image = enhanced_image.point(lambda p: 0 if p < threshold_value else 255)
        
        # Invert colors to get black text on white background (often easier for analysis)
        binary_image = ImageOps.invert(binary_image) 
        
        return binary_image

    def _find_content_area(self, binary_image: Image.Image) -> tuple:
        """
        Finds the bounding box of the main content area using horizontal and vertical projections.
        This is a heuristic for receipts that are already straight.
        Returns (left, upper, right, lower) coordinates.
        """
        width, height = binary_image.size
        pixels = np.array(binary_image) # Convert PIL Image to NumPy array

    
        horizontal_proj = np.sum(pixels, axis=1) # Sum columns for each row
        
        # Vertical projection: sum pixel intensities across rows for each column
        vertical_proj = np.sum(pixels, axis=0) # Sum rows for each column

        # Find top boundary (first row with significant text)
        top = 0
        min_density_threshold_h = width * 0.95 # e.g., if 95% of pixels are white (255) in a row, it's empty
        for i in range(height):
            if horizontal_proj[i] < min_density_threshold_h: # Found a row with text
                top = i
                break
        
        # Find bottom boundary (last row with significant text)
        bottom = height - 1
        for i in range(height - 1, -1, -1):
            if horizontal_proj[i] < min_density_threshold_h: # Found a row with text
                bottom = i + 1 # +1 to include the row
                break

        # Find left boundary (first column with significant text)
        left = 0
        min_density_threshold_v = height * 0.95 # e.g., if 95% of pixels are white (255) in a column, it's empty
        for i in range(width):
            if vertical_proj[i] < min_density_threshold_v: # Found a column with text
                left = i
                break
        
        # Find right boundary (last column with significant text)
        right = width - 1
        for i in range(width - 1, -1, -1):
            if vertical_proj[i] < min_density_threshold_v: # Found a column with text
                right = i + 1 # +1 to include the column
                break
        
        # Add some padding to the found box for better cropping
        padding = 10 
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(width, right + padding)
        bottom = min(height, bottom + padding)

        # Ensure valid box if no text found or weird projections
        if right <= left or bottom <= top:
             return (0, 0, width, height) # Return full image if detection fails
             
        return (left, top, right, bottom)

# AI DOWN

    async def process_receipt_image(self, image_b64: str) -> Dict[str, str]:
        """
        Main function to process a receipt image using Pillow:
        1. Decodes base64 image.
        2. Applies basic preprocessing.
        3. Attempts to find and crop the main content area.
        4. Generates a PDF containing the original and cropped images.
        """
        try:
            # Decode Base64 image
            try:
                # Remove data URL prefix if present
                if image_b64.startswith("data:"):
                    image_b64 = image_b64.split(",", 1)[-1]
                img_bytes = base64.b64decode(image_b64)
                original_image = Image.open(io.BytesIO(img_bytes))
                original_image = original_image.convert("RGB")
            except Exception as img_exc:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"❌ Failed to decode or open image: {img_exc}. Ensure the input is a valid base64-encoded image."))

            if original_image is None:
                raise ValueError("Could not open image from bytes.")

            # Create a copy of the original for later PDF inclusion
            original_image_for_pdf = original_image.copy()

            # Step 1: Preprocess for content detection
            binary_image_for_detection = self._preprocess_image(original_image.copy())

            # Step 2: Find the main content area
            
            content_bbox = self._find_content_area(binary_image_for_detection)
            
            # Crop the original image using the detected bounding box
            cropped_content_image = original_image.crop(content_bbox)

            # Prepare images for PDF
            pdf_images = []
            
            pdf_images.append(original_image_for_pdf)
            
            # Add the cropped content image
            pdf_images.append(cropped_content_image)

            # --- Generate PDF ---
            pdf_output_bytes = io.BytesIO()
            
            img_data_list = []
            for pil_img in pdf_images:
                img_byte_arr = io.BytesIO()
                # Save as PNG for better quality for text, or JPEG if file size is critical
                pil_img.save(img_byte_arr, format='PNG') 
                img_data_list.append(img_byte_arr.getvalue())

            pdf_bytes = img2pdf.convert(img_data_list)
            pdf_output_bytes.write(pdf_bytes)
            
            pdf_output_b64 = base64.b64encode(pdf_output_bytes.getvalue()).decode('utf-8')

            # Prepare output for MCP
            response = {
                "status": "success",
                "message": "Receipt processed. PDF generated with original and cropped content.",
                "pdf_data": pdf_output_b64,
                "cropped_content_dimensions": f"{cropped_content_image.width}x{cropped_content_image.height}"
            }
            
            return response

        except Exception as e:
            # Log the full exception for debugging
            print(f"Error during receipt processing: {e}") 
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"❌ Receipt processing failed: {e}. Ensure image is valid and receipt is relatively straight."))


# --- Create manager instance ---
receipt_processor = ReceiptProcessor()

# --- MCP Server ---
mcp = FastMCP(
    "Simplified Receipt Processing MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- MCP Tool: process_receipt_simple ---
@mcp.tool
async def process_receipt_simple(
    image_b64: Annotated[str, "Base64 encoded image of the receipt."]
) -> Dict[str, str]:
    """
    🧾 Processes a receipt image using Pillow, attempts to detect and crop the main content area,
    and returns a PDF containing the original receipt and the cropped section.
    💡 Best results for receipts photographed straight-on (no perspective correction).
    """
    return await receipt_processor.process_receipt_image(image_b64)

@mcp.tool
async def about() -> dict[str, str]:
    server_name = "PuchScan MCP"
    server_description = dedent("""
    PuchScan is a simplified receipt processing server for WhatsApp and Puch AI. It processes receipt images, detects and crops the main content, and returns a PDF with the original and cropped images, all with emoji-rich feedback.
    """)
    return {
        "name": server_name,
        "description": server_description
    }
# --- MCP Tool: help_menu ---
@mcp.tool
async def help_menu() -> str:
    """
    ℹ️ Shows a list of all available commands and their descriptions for the Receipt Processor.
    """
    return (
        "ℹ️ **Simplified Receipt Processor Help**\n"
        "🧾 - Process a receipt image (`process_receipt_simple(image_b64: str)`)\n"
        "    Provide the receipt image as a Base64 encoded string.\n"
        "    Returns a Base64 encoded PDF with the original and a simple cropped version.\n"
        "    *Note: Works best on receipts photographed straight-on.*"
    )


# --- Main Server Run Loop ---
async def main():
    """
    Starts the FastMCP server for the Simplified Receipt Processor.
    """
    print("🚀 Starting Simplified Receipt Processor MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)


if __name__ == "__main__":
    asyncio.run(main())