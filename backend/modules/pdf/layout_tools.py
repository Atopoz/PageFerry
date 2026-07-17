"""PDF 布局辅助函数。"""

from typing import Tuple


def calculate_overlap_ratio(bbox1: Tuple[float, float, float, float],
                            bbox2: Tuple[float, float, float, float]) -> float:
    """
    计算 bbox1 被 bbox2 覆盖的比例

    与 IoU 不同，这个指标只关注第一个框被覆盖的程度，用于判断文本块是否
    完全包含在某个布局区域内。

    Args:
        bbox1: 被覆盖的边界框 (x1, y1, x2, y2)
        bbox2: 覆盖的边界框 (x1, y1, x2, y2)

    Returns:
        重叠比例，范围 [0, 1]，表示 bbox1 有多少比例被 bbox2 覆盖
    """
    x1_1, y1_1, x2_1, y2_1 = bbox1
    x1_2, y1_2, x2_2, y2_2 = bbox2

    # 计算交集
    x1_inter = max(x1_1, x1_2)
    y1_inter = max(y1_1, y1_2)
    x2_inter = min(x2_1, x2_2)
    y2_inter = min(y2_1, y2_2)

    if x1_inter >= x2_inter or y1_inter >= y2_inter:
        return 0.0

    intersection = (x2_inter - x1_inter) * (y2_inter - y1_inter)
    area1 = (x2_1 - x1_1) * (y2_1 - y1_1)

    return intersection / area1 if area1 > 0 else 0.0
