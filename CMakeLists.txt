cmake_minimum_required(VERSION 3.20.6...3.22.6 FATAL_ERROR)

project(DetectionViewer)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/Stya-hr/DetectionViewer")
set(EXTENSION_CONTRIBUTORS "Huang Rui, Codex")
set(EXTENSION_DESCRIPTION "A Slicer extension to view and edit detection dataset")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/Stya-hr/DetectionViewer/refs/heads/main/DetectionViewer.png")
set(EXTENSION_SCREENSHOTURLS "https://raw.githubusercontent.com/Stya-hr/DetectionViewer/refs/heads/main/docs/figs/screenshot.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(main)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
