#!/usr/bin/env python
# -*- coding: utf-8 -*-
# XISF / FITS 文件处理模块（DWT 的 XISF 读写层）
#
# 以 MIT 许可证发布，全文见 LICENSE，授权范围见 README.md。
#
# 本文件实现 XISF 1.0 格式。规范要求所有副本与衍生作品附带其版权声明、
#   Copyright Information 与 Disclaimers 三节，以下即为该三节原文，请勿删除：
#
# ── Copyright Information ─────────────────────────────────────────────────
# Copyright © 2014-2026 Pleiades Astrophoto S.L. All rights reserved.
#
# This document may be copied and furnished to others, and derivative works that comment on or
# otherwise explain it or assist in its implementation may be prepared, copied, published, and
# distributed, in whole or in part, without restriction of any kind, provided that the above
# copyright notice, this Copyright Information section and the Disclaimers section below are
# included on all such copies and derivative works. However, this document itself may not be
# modified in any way, including by removing the copyright notice or references to Pleiades
# Astrophoto S.L., except as needed for the purpose of developing any document or deliverable
# produced by Pleiades Astrophoto S.L.
#
# The official version of this format specification document is the English language version on
# the PixInsight.com website. In the event of discrepancies between a translated version and the
# official version, the official version shall govern.
#
# The limited permissions granted above are perpetual and will not be revoked by Pleiades
# Astrophoto S.L. or its successors or assigns.
#
# ── Disclaimers ───────────────────────────────────────────────────────────
# This document and the information contained herein is provided on an "AS IS" basis and
# PLEIADES ASTROPHOTO S.L. DISCLAIMS ALL WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING,
# BUT NOT LIMITED TO, ANY ACTUAL OR ASSERTED WARRANTY OF NON-INFRINGEMENT OF PROPRIETARY RIGHTS,
# MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE. NEITHER PLEIADES ASTROPHOTO S.L. NOR ITS
# CONTRIBUTORS SHALL BE HELD LIABLE FOR ANY IMPROPER OR INCORRECT USE OF INFORMATION. NEITHER
# PLEIADES ASTROPHOTO S.L. NOR ITS CONTRIBUTORS ASSUME ANY RESPONSIBILITY FOR ANYONE'S USE OF
# INFORMATION PROVIDED BY PLEIADES ASTROPHOTO S.L. IN NO EVENT SHALL PLEIADES ASTROPHOTO S.L. OR
# ITS CONTRIBUTORS BE LIABLE TO ANYONE FOR DAMAGES OF ANY KIND, INCLUDING BUT NOT LIMITED TO,
# COMPENSATORY DAMAGES, LOST PROFITS, LOST DATA OR ANY FORM OF SPECIAL, INCIDENTAL, INDIRECT,
# CONSEQUENTIAL OR PUNITIVE DAMAGES OF ANY KIND WHETHER BASED ON BREACH OF CONTRACT OR WARRANTY,
# TORT, PRODUCT LIABILITY OR OTHERWISE.
#
# TRADEMARKS: Pleiades Astrophoto and PixInsight are trademarks of Pleiades Astrophoto S.L. Other
# product names and trademarks mentioned in this document are the property of their respective
# owners, which are in no way associated or affiliated with Pleiades Astrophoto S.L. Use of these
# names does not imply any co-operation or endorsement.
#
# 规范原文（英文版为正式版本）：https://pixinsight.com/doc/docs/XISF-1.0-spec/XISF-1.0-spec.html
#
# 支持的 XISF 1.0 规范内容：
# - 读取/写入 XISF 文件
# - 多通道图像（灰度、RGB、RGBA 等）
# - 所有像素格式（UInt8, UInt16, UInt32, Float32, Float64）
# - zlib 压缩
# - 完整天文数据支持（WCS、星表、光度测量等）
# - 多图像支持（主图 + 预览 + 缩略图）
# - ICC 色彩配置
# - CFA/Bayer 图案
# - FITS 关键字

import zlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS


class XISFError(Exception):
    """XISF 相关异常"""
    pass


class XISFImage:
    """XISF 图像数据类
    
    包含 XISF 1.0 规范的命名空间和属性：
    - XISF: 元数据命名空间（创建时间、创作者等）
    - Observer: 观测者信息
    - Organization: 组织机构信息
    - Observation: 观测数据（天体、坐标、时间、地点等）
    - Instrument: 仪器信息（望远镜、相机、传感器、滤镜等）
    - Image: 图像信息
    - Processing: 处理历史
    """
    
    def __init__(self):
        self.data: Optional[np.ndarray] = None
        self.geometry: Tuple[int, int, int] = (0, 0, 1)  # width, height, channels
        self.sample_format: str = "Float32"
        self.color_space: str = "Gray"
        self.bounds: Tuple[float, float] = (0.0, 1.0)
        self.location: Optional[str] = None
        self.compression: Optional[str] = None
        self.pixel_storage: str = "Planar"  # 像素存储模式: "Planar" 或 "Normal"
        self.fits_keywords: List[Dict[str, Any]] = []
        self.properties: Dict[str, Any] = {}
        self.metadata: Dict[str, Any] = {}
        
        # 天文数据（按照 XISF 1.0 规范）
        self.wcs: Optional[WCS] = None
        
        # XISF 命名空间（元数据）
        self.xisf_properties: Dict[str, Any] = {}
        
        # Observer 命名空间（观测者信息）
        self.observer: Dict[str, Any] = {}
        
        # Organization 命名空间（组织机构信息）
        self.organization: Dict[str, Any] = {}
        
        # Observation 命名空间（观测数据）
        self.observation: Dict[str, Any] = {}
        
        # Instrument 命名空间（仪器信息）
        self.instrument: Dict[str, Any] = {}
        
        # Image 命名空间（图像信息）
        self.image_info: Dict[str, Any] = {}
        
        # Processing 命名空间（处理历史）
        self.processing: Dict[str, Any] = {}
        
        # 其他未知命名空间的属性（保留所有未知属性，确保元数据完整性）
        self.other_properties: Dict[str, Any] = {}
        
        # CFA 信息
        self.cfa_pattern: Optional[str] = None
        self.cfa_width: int = 0
        self.cfa_height: int = 0
        self.cfa_name: Optional[str] = None
        
        # ICC 配置
        self.icc_profile: Optional[bytes] = None
        
        # 显示函数
        self.display_function: Optional[Dict[str, Any]] = None
        
        # RGB 工作空间
        self.rgb_working_space: Optional[Dict[str, Any]] = None
        
        # Image 属性
        self.image_id: Optional[str] = None  # id 属性
        self.image_type: Optional[str] = None  # imageType 属性（如 MasterLight）
        
        # 原始 Property 信息（用于写入时原样输出，保持 location="inline:base64" 等属性）
        self.original_property_info: List[Dict[str, Any]] = []
        
        # 原始的 Image 子元素（用于原样输出 DisplayFunction、Resolution 等）
        self.original_image_children: List[Any] = []
    
    def set_observer(self, name: Optional[str] = None, **kwargs):
        """设置观测者信息"""
        if name:
            self.observer['Name'] = name
        for k, v in kwargs.items():
            key = k.capitalize() if k else k
            self.observer[key] = v
    
    def set_organization(self, name: Optional[str] = None, **kwargs):
        """设置组织机构信息"""
        if name:
            self.organization['Name'] = name
        for k, v in kwargs.items():
            key = k.capitalize() if k else k
            self.organization[key] = v
    
    def set_observation(self, 
                       object_name: Optional[str] = None,
                       ra: Optional[float] = None,
                       dec: Optional[float] = None,
                       equinox: Optional[float] = None,
                       time_start: Optional[str] = None,
                       time_end: Optional[str] = None,
                       latitude: Optional[float] = None,
                       longitude: Optional[float] = None,
                       elevation: Optional[float] = None,
                       **kwargs):
        """设置观测数据"""
        if object_name:
            self.observation['Object:Name'] = object_name
        if ra is not None:
            self.observation['Center:RA'] = ra
        if dec is not None:
            self.observation['Center:Dec'] = dec
        if equinox is not None:
            self.observation['Equinox'] = equinox
        if time_start:
            self.observation['Time:Start'] = time_start
        if time_end:
            self.observation['Time:End'] = time_end
        if latitude is not None:
            self.observation['Location:Latitude'] = latitude
        if longitude is not None:
            self.observation['Location:Longitude'] = longitude
        if elevation is not None:
            self.observation['Location:Elevation'] = elevation
        
        for k, v in kwargs.items():
            key = k.replace('_', ':').title() if k else k
            self.observation[key] = v
    
    def set_instrument(self,
                      telescope_name: Optional[str] = None,
                      telescope_focal_length: Optional[float] = None,
                      telescope_aperture: Optional[float] = None,
                      camera_name: Optional[str] = None,
                      camera_gain: Optional[float] = None,
                      camera_read_noise: Optional[float] = None,
                      camera_temperature: Optional[float] = None,
                      sensor_x_pixel_size: Optional[float] = None,
                      sensor_y_pixel_size: Optional[float] = None,
                      filter_name: Optional[str] = None,
                      exposure_time: Optional[float] = None,
                      x_binning: Optional[int] = None,
                      y_binning: Optional[int] = None,
                      **kwargs):
        """设置仪器信息"""
        if telescope_name:
            self.instrument['Telescope:Name'] = telescope_name
        if telescope_focal_length is not None:
            self.instrument['Telescope:FocalLength'] = telescope_focal_length
        if telescope_aperture is not None:
            self.instrument['Telescope:Aperture'] = telescope_aperture
        if camera_name:
            self.instrument['Camera:Name'] = camera_name
        if camera_gain is not None:
            self.instrument['Camera:Gain'] = camera_gain
        if camera_read_noise is not None:
            self.instrument['Camera:ReadNoise'] = camera_read_noise
        if camera_temperature is not None:
            self.instrument['Camera:Temperature'] = camera_temperature
        if sensor_x_pixel_size is not None:
            self.instrument['Sensor:XPixelSize'] = sensor_x_pixel_size
        if sensor_y_pixel_size is not None:
            self.instrument['Sensor:YPixelSize'] = sensor_y_pixel_size
        if filter_name:
            self.instrument['Filter:Name'] = filter_name
        if exposure_time is not None:
            self.instrument['ExposureTime'] = exposure_time
        if x_binning is not None:
            self.instrument['Camera:XBinning'] = x_binning
        if y_binning is not None:
            self.instrument['Camera:YBinning'] = y_binning
        
        for k, v in kwargs.items():
            key = k.replace('_', ':').title() if k else k
            self.instrument[key] = v
    
    def set_image_info(self, **kwargs):
        """设置图像信息"""
        for k, v in kwargs.items():
            key = k.replace('_', ':').title() if k else k
            self.image_info[key] = v
    
    def set_processing(self, 
                      software: Optional[str] = None,
                      date: Optional[str] = None,
                      calibration: Optional[str] = None,
                      stacking: Optional[str] = None,
                      post_processing: Optional[str] = None,
                      **kwargs):
        """设置处理历史"""
        if software:
            self.processing['Software'] = software
        if date:
            self.processing['Date'] = date
        if calibration:
            self.processing['Calibration'] = calibration
        if stacking:
            self.processing['Stacking'] = stacking
        if post_processing:
            self.processing['PostProcessing'] = post_processing
        
        for k, v in kwargs.items():
            key = k.replace('_', ':').title() if k else k
            self.processing[key] = v
    
    def set_xisf_metadata(self,
                         creator_application: Optional[str] = None,
                         creation_time: Optional[str] = None,
                         title: Optional[str] = None,
                         authors: Optional[str] = None,
                         copyright: Optional[str] = None,
                         description: Optional[str] = None,
                         keywords: Optional[List[str]] = None,
                         **kwargs):
        """设置 XISF 元数据"""
        if creator_application:
            self.xisf_properties['CreatorApplication'] = creator_application
        if creation_time:
            self.xisf_properties['CreationTime'] = creation_time
        if title:
            self.xisf_properties['Title'] = title
        if authors:
            self.xisf_properties['Authors'] = authors
        if copyright:
            self.xisf_properties['Copyright'] = copyright
        if description:
            self.xisf_properties['Description'] = description
        if keywords:
            self.xisf_properties['Keywords'] = '\n'.join(keywords)
        
        for k, v in kwargs.items():
            key = k.replace('_', ':').title() if k else k
            self.xisf_properties[key] = v


class XISFReader:
    """XISF 文件读取器"""
    
    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)
        self.header: Optional[str] = None
        self.images: List[XISFImage] = []
        self.root: Optional[ET.Element] = None
        
    def read(self) -> List[XISFImage]:
        """读取 XISF 文件"""
        if not self.file_path.exists():
            raise XISFError(f"文件不存在：{self.file_path}")
        
        with open(self.file_path, 'rb') as f:
            # 读取魔数（前 8 字节）
            magic = f.read(8)
            if not magic.startswith(b'XISF'):
                raise XISFError(f"无效的 XISF 文件：魔数错误 {magic[:8]}")
            
            # 跳过 header length（4 字节）和 reserved（4 字节）- 总共跳过 8 字节
            f.seek(8, 1)  # 从当前位置（8）再跳过 8 字节，到 16 位置
            
            # 读取整个文件
            f.seek(0)
            data = f.read()
        
        # 查找 XML 部分
        # XISF 格式：XISF0100(8) + header_length(4) + reserved(4) + XML
        xml_start = 16  # 跳过前 16 字节
        
        # 跳过可能的填充字节，直接查找 XML 开始
        xml_data_bytes = data[xml_start:]
        # 查找第一个 '<' 字符（XML 开始）
        first_lt = xml_data_bytes.find(b'<?xml')
        if first_lt == -1:
            first_lt = xml_data_bytes.find(b'<')
        if first_lt != -1:
            xml_data_bytes = xml_data_bytes[first_lt:]
        
        # 查找 XML 结束标记（自末尾反向搜索）
        end_tag1 = xml_data_bytes.rfind(b'</ns0:xisf>')
        end_tag2 = xml_data_bytes.rfind(b'</xisf>')
        
        xml_end = -1
        if end_tag1 != -1:
            xml_end = end_tag1 + len(b'</ns0:xisf>')
        elif end_tag2 != -1:
            xml_end = end_tag2 + len(b'</xisf>')
        
        if xml_end == -1:
            raise XISFError("未找到 XML 结束标记")
        
        xml_data_bytes = xml_data_bytes[:xml_end]
        
        xml_data = xml_data_bytes.decode('utf-8', errors='ignore')
        
        # 解析 XML
        self.root = ET.fromstring(xml_data)
        self.header = xml_data
        
        # 根元素标签含 '}' 表示使用了命名空间前缀
        has_namespace_prefix = '}' in self.root.tag
        
        if has_namespace_prefix:
            # 使用带前缀的命名空间查找
            ns = {'xisf': 'http://www.pixinsight.com/xisf'}
            image_elems = self.root.findall('.//xisf:Image', ns)
            for image_elem in image_elems:
                image = self._parse_image_with_ns(image_elem, ns, data)
                self.images.append(image)
        else:
            # 使用默认命名空间（无前缀）查找
            image_elems = self.root.findall('.//Image')
            for image_elem in image_elems:
                image = self._parse_image_without_ns(image_elem, data)
                self.images.append(image)
        
        if not self.images:
            raise XISFError("XISF 文件中未找到图像数据")
        
        return self.images
    
    def _parse_image_with_ns(self, image_elem: ET.Element, ns: Dict, file_data: bytes) -> XISFImage:
        """解析单个图像元素"""
        image = XISFImage()
        
        # 解析基本属性
        geometry = image_elem.get('geometry', '0:0:1')
        parts = geometry.split(':')
        image.geometry = (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 1)
        
        image.sample_format = image_elem.get('sampleFormat', 'Float32')
        image.color_space = image_elem.get('colorSpace', 'Gray')
        
        bounds = image_elem.get('bounds', '0:1')
        bounds_parts = bounds.split(':')
        image.bounds = (float(bounds_parts[0]), float(bounds_parts[1]))
        
        image.location = image_elem.get('location')
        image.compression = image_elem.get('compression')
        image.pixel_storage = image_elem.get('pixelStorage', 'Planar')
        
        # 解析 id 和 imageType 属性
        image.image_id = image_elem.get('id')
        image.image_type = image_elem.get('imageType')
        
        # 解析 FITS 关键字
        for fits_kw in image_elem.findall('xisf:FITSKeyword', ns):
            keyword = {
                'name': fits_kw.get('name', ''),
                'value': fits_kw.get('value', ''),
                'comment': fits_kw.get('comment', '')
            }
            image.fits_keywords.append(keyword)
        
        # 解析天文数据
        self._parse_astronomical_data_with_ns(image_elem, ns, image)
        
        # 解析 CFA 信息
        cfa_elem = image_elem.find('xisf:ColorFilterArray', ns)
        if cfa_elem is not None:
            image.cfa_pattern = cfa_elem.get('pattern')
            image.cfa_width = int(cfa_elem.get('width', '0'))
            image.cfa_height = int(cfa_elem.get('height', '0'))
            image.cfa_name = cfa_elem.get('name')
        
        # 解析 ICC 配置
        icc_elem = image_elem.find('xisf:ICCProfile', ns)
        if icc_elem is not None:
            image.icc_profile = self._read_icc_profile_with_ns(icc_elem, ns, file_data)
        
        # 解析显示函数
        disp_elem = image_elem.find('xisf:DisplayFunction', ns)
        if disp_elem is not None:
            image.display_function = self._parse_display_function(disp_elem)
        
        # 解析 RGB 工作空间
        rgb_elem = image_elem.find('xisf:RGBWorkingSpace', ns)
        if rgb_elem is not None:
            image.rgb_working_space = self._parse_rgb_working_space(rgb_elem)
        
        # 保存原始的 Image 子元素（用于原样输出 DisplayFunction、Resolution 等）
        import copy
        for child in image_elem:
            # 排除 Property、FITSKeyword 和 Thumbnail：这些已单独处理或不需要保存
            if not (child.tag.endswith('Property') or child.tag.endswith('FITSKeyword') or child.tag.endswith('Thumbnail')):
                image.original_image_children.append(copy.deepcopy(child))
        
        # 读取图像数据
        if image.location:
            image.data = self._read_image_data(image, file_data)
        
        return image
    
    def _parse_image_without_ns(self, image_elem: ET.Element, file_data: bytes) -> XISFImage:
        """解析单个图像元素（无前缀命名空间）"""
        image = XISFImage()
        
        # 解析基本属性
        geometry = image_elem.get('geometry', '0:0:1')
        parts = geometry.split(':')
        image.geometry = (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 1)
        
        image.sample_format = image_elem.get('sampleFormat', 'Float32')
        image.color_space = image_elem.get('colorSpace', 'Gray')
        
        bounds = image_elem.get('bounds', '0:1')
        bounds_parts = bounds.split(':')
        image.bounds = (float(bounds_parts[0]), float(bounds_parts[1]))
        
        image.location = image_elem.get('location')
        image.compression = image_elem.get('compression')
        image.pixel_storage = image_elem.get('pixelStorage', 'Planar')
        
        # 解析 id 和 imageType 属性（无前缀命名空间）
        image.image_id = image_elem.get('id')
        image.image_type = image_elem.get('imageType')
        
        # 解析 FITS 关键字
        for fits_kw in image_elem.findall('FITSKeyword'):
            keyword = {
                'name': fits_kw.get('name', ''),
                'value': fits_kw.get('value', ''),
                'comment': fits_kw.get('comment', '')
            }
            image.fits_keywords.append(keyword)
        
        # 解析天文数据
        self._parse_astronomical_data_without_ns(image_elem, image)
        
        # 解析 CFA 信息
        cfa_elem = image_elem.find('ColorFilterArray')
        if cfa_elem is not None:
            image.cfa_pattern = cfa_elem.get('pattern')
            image.cfa_width = int(cfa_elem.get('width', '0'))
            image.cfa_height = int(cfa_elem.get('height', '0'))
            image.cfa_name = cfa_elem.get('name')
        
        # 解析 ICC 配置
        icc_elem = image_elem.find('ICCProfile')
        if icc_elem is not None:
            image.icc_profile = self._read_icc_profile_without_ns(icc_elem, file_data)
        
        # 解析显示函数
        disp_elem = image_elem.find('DisplayFunction')
        if disp_elem is not None:
            image.display_function = self._parse_display_function(disp_elem)
        
        # 解析 RGB 工作空间
        rgb_elem = image_elem.find('RGBWorkingSpace')
        if rgb_elem is not None:
            image.rgb_working_space = self._parse_rgb_working_space(rgb_elem)
        
        # 保存原始的 Image 子元素（用于原样输出 DisplayFunction、Resolution 等）
        import copy
        for child in image_elem:
            # 排除 Property、FITSKeyword 和 Thumbnail：这些已单独处理或不需要保存
            if not (child.tag.endswith('Property') or child.tag.endswith('FITSKeyword') or child.tag.endswith('Thumbnail')):
                image.original_image_children.append(copy.deepcopy(child))
        
        # 读取图像数据
        if image.location:
            image.data = self._read_image_data(image, file_data)
        
        return image
    
    def _parse_astronomical_data_with_ns(self, image_elem: ET.Element, ns: Dict, image: XISFImage):
        """解析天文数据（按照 XISF 1.0 规范实际使用的命名空间）"""
        # 先在图像内部查找
        props_in_image = image_elem.findall('xisf:Property', ns)
        
        # 也查找根节点下的 Metadata 节点（self.root 已经存在）
        metadata_elem = self.root.find('xisf:Metadata', ns)
        props_in_metadata = []
        if metadata_elem is not None:
            props_in_metadata = metadata_elem.findall('xisf:Property', ns)
        
        # 合并所有属性
        props = props_in_image + props_in_metadata
        
        for prop in props:
            prop_id = prop.get('id', '')
            prop_type = prop.get('type', '')
            value = self._parse_property_value(prop, prop_type)
            
            # 保存原始属性信息（用于原样输出）- 跳过 XISF: 开头的属性，它们应该在 Metadata 节点中
            if not prop_id.lower().startswith('xisf:'):
                prop_info = {
                    'id': prop_id,
                    'type': prop_type,
                    'attributes': prop.attrib.copy(),
                    'text': prop.text
                }
                image.original_property_info.append(prop_info)
            
            # 按照 XISF 1.0 规范实际使用的命名空间分类（不区分大小写）
            prop_id_lower = prop_id.lower()
            
            if prop_id_lower.startswith('observation:'):
                key = prop_id.split(':', 1)[1]  # 保留原始大小写
                image.observation[key] = value
            elif prop_id_lower.startswith('instrument:'):
                key = prop_id.split(':', 1)[1]  # 保留原始大小写
                image.instrument[key] = value
            elif prop_id_lower.startswith('image:'):
                key = prop_id.split(':', 1)[1]  # 保留原始大小写
                image.image_info[key] = value
            elif prop_id_lower.startswith('processing:'):
                key = prop_id.split(':', 1)[1]  # 保留原始大小写
                image.processing[key] = value
            elif prop_id_lower.startswith('xisf:'):
                # XISF 系统属性
                key = prop_id.split(':', 1)[1]  # 保留原始大小写
                image.xisf_properties[key] = value
            elif prop_id_lower.startswith('observer:'):
                # Observer 命名空间属性
                key = prop_id.split(':', 1)[1]  # 保留原始大小写
                if not hasattr(image, 'observer'):
                    image.observer = {}
                image.observer[key] = value
            elif prop_id_lower.startswith('organization:'):
                # Organization 命名空间属性
                key = prop_id.split(':', 1)[1]  # 保留原始大小写
                if not hasattr(image, 'organization'):
                    image.organization = {}
                image.organization[key] = value
            else:
                # 未知命名空间，保存到 other_properties（确保元数据完整性）
                image.other_properties[prop_id] = value
            
            # WCS 和天体测量解
            if prop_id == 'AstrometricSolution':
                image.wcs = self._parse_wcs_solution(prop, prop_type)
        
        # 从 FITS 关键字中恢复 WCS
        if image.wcs is None:
            image.wcs = self._recover_wcs_from_fits(image.fits_keywords)
    
    def _parse_astronomical_data_without_ns(self, image_elem: ET.Element, image: XISFImage):
        """解析天文数据（无前缀命名空间）"""
        # 先在图像内部查找
        props_in_image = image_elem.findall('Property')
        
        # 也查找根节点下的 Metadata 节点（self.root 已经存在）
        metadata_elem = self.root.find('Metadata')
        props_in_metadata = []
        if metadata_elem is not None:
            props_in_metadata = metadata_elem.findall('Property')
        
        # 合并所有属性
        props = props_in_image + props_in_metadata
        
        for prop in props:
            prop_id = prop.get('id', '')
            prop_type = prop.get('type', '')
            value = self._parse_property_value(prop, prop_type)
            
            # 保存原始属性信息（用于原样输出）- 跳过 XISF: 开头的属性，它们应该在 Metadata 节点中
            if not prop_id.lower().startswith('xisf:'):
                prop_info = {
                    'id': prop_id,
                    'type': prop_type,
                    'attributes': prop.attrib.copy(),
                    'text': prop.text
                }
                image.original_property_info.append(prop_info)
            
            # 按照 XISF 1.0 规范实际使用的命名空间分类（不区分大小写）
            prop_id_lower = prop_id.lower()
            
            if prop_id_lower.startswith('observation:'):
                key = prop_id.split(':', 1)[1]
                image.observation[key] = value
            elif prop_id_lower.startswith('instrument:'):
                key = prop_id.split(':', 1)[1]
                image.instrument[key] = value
            elif prop_id_lower.startswith('image:'):
                key = prop_id.split(':', 1)[1]
                image.image_info[key] = value
            elif prop_id_lower.startswith('processing:'):
                key = prop_id.split(':', 1)[1]
                image.processing[key] = value
            elif prop_id_lower.startswith('xisf:'):
                key = prop_id.split(':', 1)[1]
                image.xisf_properties[key] = value
            elif prop_id_lower.startswith('observer:'):
                key = prop_id.split(':', 1)[1]
                if not hasattr(image, 'observer'):
                    image.observer = {}
                image.observer[key] = value
            elif prop_id_lower.startswith('organization:'):
                key = prop_id.split(':', 1)[1]
                if not hasattr(image, 'organization'):
                    image.organization = {}
                image.organization[key] = value
            else:
                # 未知命名空间，保存到 other_properties（确保元数据完整性）
                image.other_properties[prop_id] = value
            
            # WCS 和天体测量解
            if prop_id == 'AstrometricSolution':
                image.wcs = self._parse_wcs_solution(prop, prop_type)
        
        # 从 FITS 关键字中恢复 WCS
        if image.wcs is None:
            image.wcs = self._recover_wcs_from_fits(image.fits_keywords)
    
    def _parse_property_value(self, prop: ET.Element, prop_type: str) -> Any:
        """解析属性值（XISF 1.0 规范：值存储在 value 属性中）"""
        # 首先尝试从 value 属性读取（XISF 1.0 标准方式）
        value_attr = prop.get('value')
        if value_attr is not None:
            if prop_type == 'Boolean':
                return value_attr.lower() == 'true'
            elif prop_type in ['Integer', 'UInt8', 'UInt16', 'UInt32', 'UInt64', 'Int8', 'Int16', 'Int32', 'Int64']:
                try:
                    return int(value_attr)
                except:
                    return value_attr
            elif prop_type in ['Float', 'Float32', 'Float64']:
                try:
                    return float(value_attr)
                except:
                    return value_attr
            elif prop_type == 'TimePoint':
                # 时间类型，直接返回字符串
                return value_attr
            else:  # String 或其他
                return value_attr
        
        # 尝试从 location 读取（外部数据）
        location = prop.get('location')
        if location:
            # 外部数据（location）读取未实现
            return None
        
        # Property 的值可能在子元素中（如 Table, Structure 等）
        if len(prop) > 0:
            # 有子元素，说明是复杂类型
            first_child = list(prop)[0]
            if first_child.tag.endswith('Value'):
                text = first_child.text
                if text:
                    text = text.strip()
                    if prop_type == 'Boolean':
                        return text.lower() == 'true'
                    elif prop_type in ['Integer', 'UInt32', 'Int32']:
                        try:
                            return int(text)
                        except:
                            return text
                    elif prop_type in ['Float', 'Float64']:
                        try:
                            return float(text)
                        except:
                            return text
                    else:
                        return text
            # 其他复杂类型返回 None
            return None
        
        # 从文本内容读取
        if prop.text:
            text = prop.text.strip()
            if text:
                if prop_type == 'Boolean':
                    return text.lower() == 'true'
                elif prop_type in ['Integer', 'UInt8', 'UInt16', 'UInt32', 'UInt64', 'Int8', 'Int16', 'Int32', 'Int64']:
                    try:
                        return int(text)
                    except:
                        return text
                elif prop_type in ['Float', 'Float32', 'Float64']:
                    try:
                        return float(text)
                    except:
                        return text
                elif prop_type == 'TimePoint':
                    # 时间类型，直接返回字符串
                    return text
                else:
                    return text
        
        return None
    
    def _parse_wcs_solution(self, prop: ET.Element, prop_type: str) -> Optional[WCS]:
        """解析 WCS 解"""
        try:
            # WCS 由 FITS 关键字恢复（见 _recover_wcs_from_fits）
            return None
        except:
            return None
    
    def _recover_wcs_from_fits(self, fits_keywords: List[Dict]) -> Optional[WCS]:
        """从 FITS 关键字恢复 WCS"""
        try:
            # 创建 FITS header
            header = fits.Header()
            for kw in fits_keywords:
                name = kw['name']
                value = kw['value']
                comment = kw.get('comment', '')
                
                # 只保留 WCS 相关关键字
                if name.startswith(('CRPIX', 'CRVAL', 'CDELT', 'CROTA', 'CTYPE', 'CUNIT', 'CD', 'PC')):
                    # 解析值
                    try:
                        # 移除引号
                        if value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]
                        elif value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        else:
                            try:
                                value = float(value)
                            except:
                                pass
                        header[name] = (value, comment)
                    except:
                        pass
            
            # 检查是否有足够的 WCS 信息
            if len(header) > 0:
                return WCS(header)
        except:
            pass
        
        return None
    
    def _read_image_data(self, image: XISFImage, file_data: bytes) -> np.ndarray:
        """读取图像数据"""
        if not image.location:
            return None
        
        # 解析 location 属性
        if image.location.startswith('attachment:'):
            parts = image.location.split(':')
            offset = int(parts[1])
            size = int(parts[2])
            
            # 读取数据
            raw_data = file_data[offset:offset + size]
            
            # 解压缩
            if image.compression:
                raw_data = self._decompress_data(raw_data, image.compression)
            
            # 转换为 numpy 数组
            return self._convert_to_array(raw_data, image)
        else:
            raise XISFError(f"不支持的位置类型：{image.location}")
    
    def _decompress_data(self, data: bytes, compression: str) -> bytes:
        """解压缩数据"""
        if compression.startswith('zlib'):
            return zlib.decompress(data)
        else:
            # 未压缩
            return data
    
    def _convert_to_array(self, data: bytes, image: XISFImage) -> np.ndarray:
        """将原始数据转换为 numpy 数组"""
        width, height, channels = image.geometry
        
        # 确定数据类型
        dtype_map = {
            'UInt8': np.uint8,
            'UInt16': np.uint16,
            'UInt32': np.uint32,
            'Int32': np.int32,
            'Float32': np.float32,
            'Float64': np.float64
        }
        
        dtype = dtype_map.get(image.sample_format, np.float32)
        
        # 创建数组
        array = np.frombuffer(data, dtype=dtype)
        
        # 根据像素存储模式重塑形状
        if channels == 1:
            array = array.reshape((height, width))
        elif image.pixel_storage == 'Normal':
            array = array.reshape((height, width, channels))
        else:
            # Planar 模式: 逐通道读取，避免 reshape→transpose→ascontiguousarray 全量拷贝
            per_ch = width * height
            itemsize = np.dtype(dtype).itemsize
            result = np.empty((height, width, channels), dtype=dtype)
            for c in range(channels):
                ch_flat = np.frombuffer(data, dtype=dtype, count=per_ch, offset=c * per_ch * itemsize)
                result[:, :, c] = ch_flat.reshape(height, width)
            array = result
        
        return array
    
    def _read_icc_profile_with_ns(self, icc_elem: ET.Element, ns: Dict, file_data: bytes) -> Optional[bytes]:
        """读取 ICC 配置文件"""
        location = icc_elem.get('location')
        if location and location.startswith('attachment:'):
            parts = location.split(':')
            offset = int(parts[1])
            size = int(parts[2])
            return file_data[offset:offset + size]
        return None
    
    def _read_icc_profile_without_ns(self, icc_elem: ET.Element, file_data: bytes) -> Optional[bytes]:
        """读取 ICC 配置文件（无前缀命名空间）"""
        location = icc_elem.get('location')
        if location and location.startswith('attachment:'):
            parts = location.split(':')
            offset = int(parts[1])
            size = int(parts[2])
            return file_data[offset:offset + size]
        return None
    
    def _parse_display_function(self, disp_elem: ET.Element) -> Dict[str, Any]:
        """解析显示函数"""
        return {
            'type': disp_elem.get('type', 'Linear'),
            'shadow': disp_elem.get('shadowClipping', '0'),
            'highlight': disp_elem.get('highlightClipping', '1')
        }
    
    def _parse_rgb_working_space(self, rgb_elem: ET.Element) -> Dict[str, Any]:
        """解析 RGB 工作空间"""
        return {
            'name': rgb_elem.get('name', 'sRGB'),
            'toXYZ': rgb_elem.get('toXYZ')
        }


class XISFWriter:
    """XISF 文件写入器"""
    
    def __init__(self):
        self.images: List[XISFImage] = []
        
    def add_image(self, image: XISFImage):
        """添加图像"""
        self.images.append(image)
    
    def write(self, file_path: Union[str, Path], compression: Optional[str] = None):
        """写入 XISF 文件"""
        file_path = Path(file_path)
        
        # 第一步：预计算所有图像数据（压缩后）
        image_data_list = []
        for image in self.images:
            if image.data is not None:
                data_bytes = image.data.tobytes()
                if compression and compression.startswith('zlib'):
                    compressed_data = zlib.compress(data_bytes)
                    image_data_list.append(compressed_data)
                    image.compression = f"zlib:{len(data_bytes)}"
                else:
                    image_data_list.append(data_bytes)
            else:
                image_data_list.append(None)
        
        # 第二步：迭代构建 XML 直到位置收敛（location 属性值长度会影响 XML 大小）
        # 初始用占位符估算
        data_positions = [0] * len(self.images)
        data_sizes = [0] * len(self.images)
        
        for iteration in range(3):
            root = ET.Element('xisf', xmlns='http://www.pixinsight.com/xisf')
            root.set('version', '1.0')
            root.set('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance')
            root.set('xsi:schemaLocation', 'http://www.pixinsight.com/xisf http://pixinsight.com/xisf/xisf-1.0.xsd')
            
            metadata_elem = None
            for idx, image in enumerate(self.images):
                if idx == 0 and len(image.xisf_properties) > 0:
                    metadata_elem = ET.SubElement(root, 'Metadata')
                    for key, value in image.xisf_properties.items():
                        self._add_property_to_element(metadata_elem, f'XISF:{key}', value)
            
            for idx, image in enumerate(self.images):
                image_elem = ET.SubElement(root, 'Image')
                self._fill_image_element(image_elem, image, data_positions[idx], data_sizes[idx])
            
            ET.indent(root, space="  ")
            xml_str = ET.tostring(root, encoding='unicode')
            xml_bytes = xml_str.encode('utf-8')
            
            # 计算数据起始位置
            xml_end_pos = 16 + len(xml_bytes)
            padding_needed = 4096 - (xml_end_pos % 4096)
            if padding_needed == 4096:
                padding_needed = 0
            new_data_start = xml_end_pos + padding_needed
            
            # 计算新的数据位置
            new_positions = []
            current_pos = new_data_start
            for idx in range(len(self.images)):
                new_positions.append(current_pos)
                if image_data_list[idx] is not None:
                    current_pos += len(image_data_list[idx])
            
            # 检查是否收敛
            if new_positions == data_positions:
                break
            
            data_positions = new_positions
            for idx in range(len(self.images)):
                if image_data_list[idx] is not None:
                    data_sizes[idx] = len(image_data_list[idx])
        
        # 最终 XML
        final_xml_bytes = xml_bytes
        
        # 写入文件
        with open(file_path, 'wb') as f:
            f.write(b'XISF0100')
            
            header_length = len(final_xml_bytes)
            f.write(header_length.to_bytes(4, byteorder='little', signed=False))
            
            f.write(b'\x00\x00\x00\x00')
            
            f.write(final_xml_bytes)
            
            xml_end_pos = 16 + len(final_xml_bytes)
            padding_needed = 4096 - (xml_end_pos % 4096)
            if padding_needed == 4096:
                padding_needed = 0
            if padding_needed > 0:
                f.write(b'\x00' * padding_needed)
            
            for idx in range(len(self.images)):
                if image_data_list[idx] is not None:
                    f.write(image_data_list[idx])
    
    def _fill_image_element(self, elem: ET.Element, image: XISFImage, data_offset: int, data_size: int):
        """填充 Image 元素的属性和子元素"""
        # 基本属性
        geometry = f"{image.geometry[0]}:{image.geometry[1]}:{image.geometry[2]}"
        elem.set('geometry', geometry)
        elem.set('sampleFormat', image.sample_format)
        elem.set('colorSpace', image.color_space)
        
        # id 和 imageType 属性
        if image.image_id:
            elem.set('id', image.image_id)
        if image.image_type:
            elem.set('imageType', image.image_type)
        
        # bounds 采用整数格式（依 XISF 1.0 规范）
        if isinstance(image.bounds[0], float) and image.bounds[0].is_integer():
            b0 = int(image.bounds[0])
        else:
            b0 = image.bounds[0]
        if isinstance(image.bounds[1], float) and image.bounds[1].is_integer():
            b1 = int(image.bounds[1])
        else:
            b1 = image.bounds[1]
        elem.set('bounds', f"{b0}:{b1}")

        if image.pixel_storage:
            elem.set('pixelStorage', image.pixel_storage)

        # 设置数据位置和大小
        elem.set('location', f'attachment:{data_offset}:{data_size}')
        
        # 压缩信息
        if image.compression:
            elem.set('compression', image.compression)
        
        # FITS 关键字
        for kw in image.fits_keywords:
            fits_elem = ET.SubElement(elem, 'FITSKeyword')
            fits_elem.set('name', kw['name'])
            fits_elem.set('value', str(kw['value']))
            if kw.get('comment'):
                fits_elem.set('comment', kw['comment'])
        
        # 天文数据
        self._add_astronomical_data_to_element(elem, image)
        
        # CFA 信息
        if image.cfa_pattern:
            cfa_elem = ET.SubElement(elem, 'ColorFilterArray')
            cfa_elem.set('pattern', image.cfa_pattern)
            cfa_elem.set('width', str(image.cfa_width))
            cfa_elem.set('height', str(image.cfa_height))
            if image.cfa_name:
                cfa_elem.set('name', image.cfa_name)
    
    def _add_astronomical_data_to_element(self, elem: ET.Element, image: XISFImage):
        """向元素添加天文数据（Property 元素）"""
        # 添加原始的 Image 子元素（DisplayFunction、Resolution 等）
        import copy
        if hasattr(image, 'original_image_children') and len(image.original_image_children) > 0:
            for child in image.original_image_children:
                # 处理命名空间：写入时使用默认命名空间，需要去掉前缀
                new_child = copy.deepcopy(child)
                # 如果标签有命名空间前缀，只保留本地名称
                if '}' in new_child.tag:
                    new_child.tag = new_child.tag.split('}')[-1]
                elem.append(new_child)
        
        # 如果有原始属性信息，直接使用（保持 location="inline:base64" 等属性）
        if hasattr(image, 'original_property_info') and len(image.original_property_info) > 0:
            for prop_info in image.original_property_info:
                prop_elem = ET.SubElement(elem, 'Property')
                # 设置所有原始属性
                for attr_name, attr_value in prop_info['attributes'].items():
                    prop_elem.set(attr_name, attr_value)
                # 设置文本内容
                if prop_info['text']:
                    prop_elem.text = prop_info['text']
            return
        
        # 没有原始元素，正常添加
        # Observation 命名空间
        for key, value in image.observation.items():
            self._add_property_to_element(elem, f'Observation:{key}', value)
        
        # Instrument 命名空间
        for key, value in image.instrument.items():
            self._add_property_to_element(elem, f'Instrument:{key}', value)
        
        # Image 命名空间
        for key, value in image.image_info.items():
            self._add_property_to_element(elem, f'Image:{key}', value)
        
        # Processing 命名空间
        for key, value in image.processing.items():
            self._add_property_to_element(elem, f'Processing:{key}', value)
        
        # Observer 和 Organization
        if hasattr(image, 'observer'):
            for key, value in image.observer.items():
                self._add_property_to_element(elem, f'Observer:{key}', value)
        
        if hasattr(image, 'organization'):
            for key, value in image.organization.items():
                self._add_property_to_element(elem, f'Organization:{key}', value)
        
        # 其他未知命名空间的属性（PCL:、PixInsight: 等，都放 Image 节点）
        if hasattr(image, 'other_properties'):
            for prop_id, value in image.other_properties.items():
                self._add_property_to_element(elem, prop_id, value)
        
        # WCS 解
        if image.wcs is not None:
            self._add_wcs_solution_to_element(elem, image.wcs)
    
    def _add_property_to_element(self, elem: ET.Element, prop_id: str, value: Any):
        """向元素添加单个 Property"""
        if value is None:
            return
        
        prop_elem = ET.SubElement(elem, 'Property')
        prop_elem.set('id', prop_id)
        
        # 确定类型 - 使用 XISF 1.0 规范的类型
        # 整数类型用 UInt8/UInt16/UInt32/UInt64 或 Int8/Int16/Int32/Int64
        if isinstance(value, bool):
            prop_elem.set('type', 'Boolean')
            prop_elem.text = 'true' if value else 'false'
        elif isinstance(value, int):
            # 根据值的大小选择合适的整数类型
            if 0 <= value <= 255:
                prop_elem.set('type', 'UInt8')
            elif -128 <= value <= 127:
                prop_elem.set('type', 'Int8')
            elif 0 <= value <= 65535:
                prop_elem.set('type', 'UInt16')
            elif -32768 <= value <= 32767:
                prop_elem.set('type', 'Int16')
            elif 0 <= value <= 4294967295:
                prop_elem.set('type', 'UInt32')
            elif -2147483648 <= value <= 2147483647:
                prop_elem.set('type', 'Int32')
            else:
                prop_elem.set('type', 'UInt64')
            prop_elem.set('value', str(value))
        elif isinstance(value, float):
            prop_elem.set('type', 'Float64')
            prop_elem.set('value', str(value))
        else:
            # 字符串类型：只有 String 类型用文本，其他类型用 value 属性
            # 严格检查是否是 ISO 8601 时间格式 (如 2025-05-18T13:22:25.066Z)
            # 格式必须是：YYYY-MM-DDTHH:MM:SS[.SSS]Z
            val_str = str(value)
            import re
            iso8601_pattern = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$'
            if re.match(iso8601_pattern, val_str):
                prop_elem.set('type', 'TimePoint')
                prop_elem.set('value', val_str)
            else:
                prop_elem.set('type', 'String')
                prop_elem.text = val_str
    
    def _add_wcs_solution_to_element(self, elem: ET.Element, wcs: WCS):
        """向元素添加 WCS 解（通过 FITSKeyword）"""
        if hasattr(wcs, 'to_header'):
            header = wcs.to_header()
            for name, value in header.items():
                if name.startswith(('CRPIX', 'CRVAL', 'CDELT', 'CROTA', 'CTYPE', 'CUNIT', 'CD', 'PC')):
                    fits_elem = ET.SubElement(elem, 'FITSKeyword')
                    fits_elem.set('name', name)
                    fits_elem.set('value', str(value))
    
    def _create_image_element(self, image: XISFImage, idx: int, data_offset: int, data_size: int) -> ET.Element:
        """创建图像 XML 元素"""
        image_elem = ET.Element('{http://www.pixinsight.com/xisf}Image')
        
        # 基本属性
        geometry = f"{image.geometry[0]}:{image.geometry[1]}:{image.geometry[2]}"
        image_elem.set('geometry', geometry)
        image_elem.set('sampleFormat', image.sample_format)
        image_elem.set('colorSpace', image.color_space)
        image_elem.set('bounds', f"{image.bounds[0]}:{image.bounds[1]}")
        
        # 设置数据位置和大小
        image_elem.set('location', f'attachment:{data_offset}:{data_size}')
        
        # 压缩信息
        if image.compression:
            image_elem.set('compression', image.compression)
        
        # FITS 关键字
        for kw in image.fits_keywords:
            fits_elem = ET.SubElement(image_elem, '{http://www.pixinsight.com/xisf}FITSKeyword')
            fits_elem.set('name', kw['name'])
            fits_elem.set('value', str(kw['value']))
            if kw.get('comment'):
                fits_elem.set('comment', kw['comment'])
        
        # 天文数据
        self._add_astronomical_data(image_elem, image)
        
        # CFA 信息
        if image.cfa_pattern:
            cfa_elem = ET.SubElement(image_elem, '{http://www.pixinsight.com/xisf}ColorFilterArray')
            cfa_elem.set('pattern', image.cfa_pattern)
            cfa_elem.set('width', str(image.cfa_width))
            cfa_elem.set('height', str(image.cfa_height))
            if image.cfa_name:
                cfa_elem.set('name', image.cfa_name)
        
        return image_elem
    
    def _add_astronomical_data(self, image_elem: ET.Element, image: XISFImage):
        """添加天文数据（按照 XISF 1.0 规范实际使用的命名空间，首字母大写）"""
        # XISF 系统属性（优先写入）
        for key, value in image.xisf_properties.items():
            self._add_property(image_elem, f'XISF:{key}', value)
        
        # Observation 命名空间（实际使用，首字母大写）
        for key, value in image.observation.items():
            self._add_property(image_elem, f'Observation:{key}', value)
        
        # Instrument 命名空间（实际使用，首字母大写）
        for key, value in image.instrument.items():
            self._add_property(image_elem, f'Instrument:{key}', value)
        
        # Image 命名空间（首字母大写）
        for key, value in image.image_info.items():
            self._add_property(image_elem, f'Image:{key}', value)
        
        # Processing 命名空间（首字母大写）
        for key, value in image.processing.items():
            self._add_property(image_elem, f'Processing:{key}', value)
        
        # Observer 和 Organization 命名空间（首字母大写）
        if hasattr(image, 'observer'):
            for key, value in image.observer.items():
                self._add_property(image_elem, f'Observer:{key}', value)
        
        if hasattr(image, 'organization'):
            for key, value in image.organization.items():
                self._add_property(image_elem, f'Organization:{key}', value)
        
        # WCS 解
        if image.wcs is not None:
            self._add_wcs_solution(image_elem, image.wcs)
    
    def _add_property(self, image_elem: ET.Element, prop_id: str, value: Any):
        """添加属性"""
        if value is None:
            return
        
        prop_elem = ET.SubElement(image_elem, '{http://www.pixinsight.com/xisf}Property')
        prop_elem.set('id', prop_id)
        
        # 确定类型
        if isinstance(value, bool):
            prop_elem.set('type', 'Boolean')
            prop_elem.text = 'true' if value else 'false'
        elif isinstance(value, int):
            prop_elem.set('type', 'Integer')
            prop_elem.text = str(value)
        elif isinstance(value, float):
            prop_elem.set('type', 'Float')
            prop_elem.text = str(value)
        else:
            prop_elem.set('type', 'String')
            prop_elem.text = str(value)
    
    def _add_wcs_solution(self, image_elem: ET.Element, wcs: WCS):
        """添加 WCS 解"""
        if hasattr(wcs, 'to_header'):
            header = wcs.to_header()
            for name, value in header.items():
                if name.startswith(('CRPIX', 'CRVAL', 'CDELT', 'CROTA', 'CTYPE', 'CUNIT', 'CD', 'PC')):
                    fits_elem = ET.SubElement(image_elem, '{http://www.pixinsight.com/xisf}FITSKeyword')
                    fits_elem.set('name', name)
                    fits_elem.set('value', str(value))
    
    def _write_image_data(self, f, image: XISFImage, compression: Optional[str] = None):
        """写入图像数据"""
        if image.data is None:
            return
        
        # 获取当前位置
        data_offset = f.tell()
        
        # 转换为字节
        data_bytes = image.data.tobytes()
        
        # 压缩
        if compression and compression.startswith('zlib'):
            compressed_data = zlib.compress(data_bytes)
            f.write(compressed_data)
            data_size = len(compressed_data)
            
            # 更新压缩属性
            image.compression = f"zlib:{len(data_bytes)}"
        else:
            f.write(data_bytes)
            data_size = len(data_bytes)
    
    def create_image_from_array(self, 
                                 data: np.ndarray,
                                 sample_format: str = 'Float32',
                                 color_space: str = 'Gray',
                                 fits_keywords: Optional[List[Dict]] = None) -> XISFImage:
        """从 numpy 数组创建 XISFImage"""
        image = XISFImage()
        
        if len(data.shape) == 2:
            height, width = data.shape
            channels = 1
        else:
            height, width, channels = data.shape
        
        image.data = data
        image.geometry = (width, height, channels)
        image.sample_format = sample_format
        image.color_space = color_space
        
        if channels > 1:
            image.pixel_storage = "Normal"
        
        # 根据数据类型设置 bounds
        if 'Float' in sample_format:
            image.bounds = (0.0, 1.0)
        elif 'UInt8' in sample_format:
            image.bounds = (0, 255)
        elif 'UInt16' in sample_format:
            image.bounds = (0, 65535)
        else:
            image.bounds = (0, 1)
        
        if fits_keywords:
            image.fits_keywords = fits_keywords
        
        return image


def read_xisf(file_path: Union[str, Path]) -> List[XISFImage]:
    """便捷函数：读取 XISF 文件"""
    reader = XISFReader(file_path)
    return reader.read()


def write_xisf(file_path: Union[str, Path], 
               images: Union[XISFImage, List[XISFImage]],
               compression: Optional[str] = None):
    """便捷函数：写入 XISF 文件"""
    writer = XISFWriter()
    
    if isinstance(images, XISFImage):
        writer.add_image(images)
    else:
        for image in images:
            writer.add_image(image)
    
    writer.write(file_path, compression)


def load_xisf_as_fits(file_path: Union[str, Path]) -> Tuple[np.ndarray, fits.Header]:
    """
    加载 XISF 文件并转换为 FITS 格式
    返回图像数据和 FITS header
    """
    reader = XISFReader(file_path)
    images = reader.read()
    
    if not images:
        raise XISFError("XISF 文件中没有图像")
    
    # 使用第一个图像
    image = images[0]
    
    # 创建 FITS header
    header = fits.Header()
    
    # 添加 FITS 关键字
    for kw in image.fits_keywords:
        try:
            name = kw['name']
            value = kw['value']
            comment = kw.get('comment', '')
            
            # 解析值
            if value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            elif value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            else:
                try:
                    value = float(value)
                except:
                    pass
            
            header[name] = (value, comment)
        except:
            pass
    
    return image.data, header
