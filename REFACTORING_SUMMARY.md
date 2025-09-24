# Crypto Widget Refactoring: Electron to Python Tkinter

This document summarizes the refactoring work done to replace the Electron frontend with a Python Tkinter implementation.

## Overview

The original application was built using Electron with HTML/CSS/JavaScript for the frontend. We've successfully refactored it to use Python with Tkinter for the GUI while maintaining the same functionality.

## Files Created

1. **crypto_data.py** - Python module for fetching cryptocurrency data from CoinGecko API
2. **crypto_widget.py** - Main Tkinter application with all GUI functionality
3. **requirements.txt** - Python dependencies (requests)
4. **run_python.sh** - Script to run the Python application
5. **README_PYTHON.md** - Documentation for the Python version
6. **Updated package.json** - Added "python" script to run the Python version

## Key Features Implemented

### 1. Cryptocurrency Data Fetching
- Implemented `CryptoDataFetcher` class that mirrors the functionality of the original Electron version
- Fetches real-time prices for BTC, ETH, ADA, DOT, and LINK from CoinGecko API
- Handles errors gracefully

### 2. Tkinter UI
- Created a borderless, always-on-top window similar to the Electron version
- Implemented horizontal scrolling for cryptocurrency data
- Designed visual elements that match the original styling:
  - Triangle collapse/expand button
  - Color-coded price change indicators (green for positive, red for negative)
  - Clean, minimal design

### 3. Window Management
- **Dragging**: Click anywhere on the window to drag it (similar to `-webkit-app-region: drag`)
- **Collapse/Expand**: Toggle button to collapse the widget to a small square or expand it to full size
- **Always on Top**: Window stays on top of other applications

### 4. User Interaction
- **Context Menu**: Right-click anywhere to access the context menu with options:
  - Add Cryptocurrency (placeholder)
  - Remove Cryptocurrency (placeholder)
  - Refresh Prices (manual update)
  - Quit (exit application)
- **Scrolling**: Horizontal scrolling through cryptocurrency data using mouse wheel or trackpad

### 5. Data Updates
- Automatic price updates every 30 seconds using threading
- Manual refresh option through context menu

## How to Run

### Option 1: Direct Python execution
```bash
# Install dependencies
pip3 install -r requirements.txt

# Run with system Python (which has Tkinter)
/usr/bin/python3 crypto_widget.py
```

### Option 2: Using the run script
```bash
# Make script executable
chmod +x run_python.sh

# Run the script
./run_python.sh
```

### Option 3: Using npm
```bash
npm run python
```

## Technical Implementation Details

### Architecture
The Python version follows a similar architecture to the Electron version:
- Separation of data fetching (`CryptoDataFetcher`) and UI (`CryptoWidget`)
- Event-driven updates using threading for periodic data refresh
- Callback-based UI updates

### Key Differences from Electron Version
1. **Window Management**: Uses Tkinter's `overrideredirect()` instead of Electron's frameless window options
2. **Dragging**: Implemented with mouse event bindings instead of CSS `-webkit-app-region`
3. **Rendering**: Uses Tkinter widgets instead of HTML/CSS
4. **Packaging**: No need for Electron packaging; can run directly with Python

### UI Components
1. **Main Window**: Borderless, draggable window with fixed height
2. **Collapse Button**: Triangle button that toggles between collapsed/expanded states
3. **Crypto Display**: Horizontal scrolling area with cryptocurrency data
4. **Context Menu**: Right-click menu with application controls

## Dependencies
- Python 3.6+
- requests (for API calls)
- tkinter (for GUI - included with most Python installations)

## Future Improvements
1. Add actual implementation for "Add Cryptocurrency" and "Remove Cryptocurrency" features
2. Implement configuration saving/loading
3. Add more customization options (colors, size, update frequency)
4. Package as a standalone application using tools like PyInstaller

## Testing
The application has been tested on macOS and successfully:
- Fetches cryptocurrency data from CoinGecko API
- Displays data in a draggable, collapsible widget
- Handles user interactions (dragging, collapsing, context menu)
- Updates prices automatically every 30 seconds

## Conclusion
The refactoring from Electron to Python Tkinter was successful. The Python version maintains all the core functionality of the original while offering a simpler runtime environment without the need for Node.js or Electron dependencies.