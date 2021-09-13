"""
Simple OpenStreetMap renderer.
"""
import argparse
import logging
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import svgwrite
from colour import Color
from svgwrite.container import Group
from svgwrite.path import Path as SVGPath
from svgwrite.shapes import Rect

from map_machine.boundary_box import BoundaryBox
from map_machine.constructor import Constructor
from map_machine.figure import Road, StyledFigure
from map_machine.flinger import Flinger
from map_machine.icon import ShapeExtractor
from map_machine.map_configuration import LabelMode, MapConfiguration
from map_machine.osm_getter import NetworkError, get_osm
from map_machine.osm_reader import OSMData, OSMNode, OSMReader, OverpassReader
from map_machine.point import Occupied, Point
from map_machine.road import Intersection, RoadPart
from map_machine.scheme import Scheme
from map_machine.ui import BuildingMode, progress_bar
from map_machine.workspace import workspace

__author__ = "Sergey Vartanov"
__email__ = "me@enzet.ru"


class Map:
    """
    Map drawing.
    """

    def __init__(
        self,
        flinger: Flinger,
        svg: svgwrite.Drawing,
        scheme: Scheme,
        configuration: MapConfiguration,
    ) -> None:
        self.flinger: Flinger = flinger
        self.svg: svgwrite.Drawing = svg
        self.scheme: Scheme = scheme
        self.configuration = configuration

        self.background_color: Color = self.scheme.get_color("background_color")
        if self.configuration.is_wireframe():
            self.background_color: Color = Color("#111111")

    def draw(self, constructor: Constructor) -> None:
        """Draw map."""
        self.svg.add(
            Rect((0, 0), self.flinger.size, fill=self.background_color)
        )
        ways: list[StyledFigure] = sorted(
            constructor.figures, key=lambda x: x.line_style.priority
        )
        ways_length: int = len(ways)
        for index, way in enumerate(ways):
            progress_bar(index, ways_length, step=10, text="Drawing ways")
            path_commands: str = way.get_path(self.flinger)
            if path_commands:
                path: SVGPath = SVGPath(d=path_commands)
                path.update(way.line_style.style)
                self.svg.add(path)
        progress_bar(-1, 0, text="Drawing ways")

        layered_roads: dict[float, list[Road]] = {}
        for road in constructor.roads:
            if road.layer not in layered_roads:
                layered_roads[road.layer] = []
            layered_roads[road.layer].append(road)

        for layer in sorted(layered_roads.keys()):
            roads = layered_roads[layer]
            for road in roads:
                road.draw(self.svg, self.flinger, road.matcher.border_color, 2)
            for road in roads:
                road.draw(self.svg, self.flinger, road.matcher.color)

        for tree in constructor.trees:
            tree.draw(self.svg, self.flinger, self.scheme)
        for tree in constructor.craters:
            tree.draw(self.svg, self.flinger)

        for direction_sector in constructor.direction_sectors:
            direction_sector.draw(self.svg, self.scheme)

        self.draw_buildings(constructor)

        # All other points

        occupied: Optional[Occupied]
        if self.configuration.overlap == 0:
            occupied = None
        else:
            occupied = Occupied(
                self.flinger.size[0],
                self.flinger.size[1],
                self.configuration.overlap,
            )

        nodes: list[Point] = sorted(
            constructor.points, key=lambda x: -x.priority
        )
        steps: int = len(nodes)

        for index, node in enumerate(nodes):
            progress_bar(index, steps * 3, step=10, text="Drawing main icons")
            node.draw_main_shapes(self.svg, occupied)

        for index, point in enumerate(nodes):
            progress_bar(
                steps + index, steps * 3, step=10, text="Drawing extra icons"
            )
            point.draw_extra_shapes(self.svg, occupied)

        for index, point in enumerate(nodes):
            progress_bar(
                steps * 2 + index, steps * 3, step=10, text="Drawing texts"
            )
            if (
                not self.configuration.is_wireframe()
                and self.configuration.label_mode != LabelMode.NO
            ):
                point.draw_texts(
                    self.svg, occupied, self.configuration.label_mode
                )

        progress_bar(-1, len(nodes), step=10, text="Drawing nodes")

    def draw_buildings(self, constructor: Constructor) -> None:
        """Draw buildings: shade, walls, and roof."""
        if self.configuration.building_mode == BuildingMode.FLAT:
            for building in constructor.buildings:
                building.draw(self.svg, self.flinger)
            return

        scale: float = self.flinger.get_scale() / 3.0
        building_shade: Group = Group(opacity=0.1)
        for building in constructor.buildings:
            building.draw_shade(building_shade, self.flinger)
        self.svg.add(building_shade)

        previous_height: float = 0
        count: int = len(constructor.heights)
        for index, height in enumerate(sorted(constructor.heights)):
            progress_bar(index, count, step=1, text="Drawing buildings")
            fill: Color()
            for building in constructor.buildings:
                if building.height < height or building.min_height > height:
                    continue
                building.draw_walls(self.svg, height, previous_height, scale)

            for building in constructor.buildings:
                if building.height == height:
                    building.draw_roof(self.svg, self.flinger, scale)

            previous_height = height

        progress_bar(-1, count, step=1, text="Drawing buildings")

    def draw_roads(self, roads: Iterator[Road]) -> None:
        """Draw road as simple SVG path."""
        nodes: dict[OSMNode, set[RoadPart]] = {}

        for road in roads:
            for index in range(len(road.outers[0]) - 1):
                node_1: OSMNode = road.outers[0][index]
                node_2: OSMNode = road.outers[0][index + 1]
                point_1: np.ndarray = self.flinger.fling(node_1.coordinates)
                point_2: np.ndarray = self.flinger.fling(node_2.coordinates)
                scale: float = self.flinger.get_scale(node_1.coordinates)
                part_1: RoadPart = RoadPart(point_1, point_2, road.lanes, scale)
                part_2: RoadPart = RoadPart(point_2, point_1, road.lanes, scale)
                # part_1.draw_normal(self.svg)

                for node in node_1, node_2:
                    if node not in nodes:
                        nodes[node] = set()

                nodes[node_1].add(part_1)
                nodes[node_2].add(part_2)

        for node in nodes:
            parts: set[RoadPart] = nodes[node]
            if len(parts) < 4:
                continue
            intersection: Intersection = Intersection(list(parts))
            intersection.draw(self.svg, True)


def ui(options: argparse.Namespace) -> None:
    """
    Map Machine entry point.

    :param options: command-line arguments
    """
    configuration: MapConfiguration = MapConfiguration.from_options(
        options, int(options.zoom)
    )
    if not options.boundary_box and not options.input_file_name:
        logging.fatal("Specify either --boundary-box, or --input.")
        exit(1)

    if options.boundary_box:
        boundary_box: BoundaryBox = BoundaryBox.from_text(options.boundary_box)

    cache_path: Path = Path(options.cache)
    cache_path.mkdir(parents=True, exist_ok=True)

    input_file_names: list[Path]

    if options.input_file_name:
        input_file_names = list(map(Path, options.input_file_name))
    else:
        try:
            cache_file_path: Path = (
                cache_path / f"{boundary_box.get_format()}.osm"
            )
            get_osm(boundary_box, cache_file_path)
        except NetworkError as e:
            logging.fatal(e.message)
            exit(1)
        input_file_names = [cache_file_path]

    scheme: Scheme = Scheme(workspace.DEFAULT_SCHEME_PATH)
    min_: np.ndarray
    max_: np.ndarray
    osm_data: OSMData
    view_box: BoundaryBox

    if input_file_names[0].name.endswith(".json"):
        reader: OverpassReader = OverpassReader()
        reader.parse_json_file(input_file_names[0])

        osm_data = reader.osm_data
        view_box = boundary_box
    else:
        osm_reader = OSMReader()

        for file_name in input_file_names:
            if not file_name.is_file():
                logging.fatal(f"No such file: {file_name}.")
                exit(1)

            osm_reader.parse_osm_file(file_name)

        osm_data = osm_reader.osm_data

        if options.boundary_box:
            view_box = boundary_box
        else:
            view_box = osm_data.view_box

    flinger: Flinger = Flinger(view_box, options.zoom, osm_data.equator_length)
    size: np.ndarray = flinger.size

    svg: svgwrite.Drawing = svgwrite.Drawing(
        options.output_file_name, size=size
    )
    icon_extractor: ShapeExtractor = ShapeExtractor(
        workspace.ICONS_PATH, workspace.ICONS_CONFIG_PATH
    )

    constructor: Constructor = Constructor(
        osm_data=osm_data,
        flinger=flinger,
        scheme=scheme,
        extractor=icon_extractor,
        configuration=configuration,
    )
    constructor.construct()

    painter: Map = Map(
        flinger=flinger, svg=svg, scheme=scheme, configuration=configuration
    )
    painter.draw(constructor)

    logging.info(f"Writing output SVG to {options.output_file_name}...")
    with open(options.output_file_name, "w") as output_file:
        svg.write(output_file)
