//! 检测框绘制：直接在原始 BGR 帧上画框与标签，再由 FFmpeg 编码成 HLS。
//!
//! 内置 5x7 点阵字体，避免依赖 GDI/字体文件；非 ASCII 字符（如中文类别名）
//! 与 OpenCV `putText` 行为一致，显示为 `?`。
//!
//! 本模块已从 `fireguardd` 迁入 `fg-media`，只依赖 `fg-domain` 的检测值对象。

use fg_domain::Detection;

const FONT_SCALE: usize = 2;
const CHAR_W: usize = 5;
const CHAR_H: usize = 7;
const CHAR_SPACING: usize = 1;

fn color_for(class_name: &str) -> [u8; 3] {
    match class_name.to_ascii_lowercase().as_str() {
        "fire" => [0, 60, 255],
        "smoke" => [80, 80, 80],
        "person" => [255, 165, 0],
        "car" => [255, 0, 255],
        _ => [0, 165, 255],
    }
}

/// 在帧上绘制所有检测框。`buf` 为 BGR24 连续内存。
pub fn draw_detections(buf: &mut [u8], width: usize, height: usize, detections: &[Detection]) {
    for det in detections {
        let [x1, y1, x2, y2] = det.bbox;
        let color = color_for(&det.class_name);
        draw_rect(buf, width, height, x1, y1, x2, y2, color, 2);

        // 标签顺序：目标 ID → 类别 → 置信度，与右侧事件面板保持一致。
        let label = match det.track_id {
            Some(id) => format!(
                "#{} {} {:.2}",
                id,
                det.class_name.to_uppercase(),
                det.confidence
            ),
            None => format!("{} {:.2}", det.class_name.to_uppercase(), det.confidence),
        };
        let text: String = label
            .chars()
            .map(|c| {
                if c.is_ascii_graphic() || c == ' ' {
                    c
                } else {
                    '?'
                }
            })
            .collect();

        let text_w = text.chars().count() * (CHAR_W + CHAR_SPACING) * FONT_SCALE;
        let bar_h = CHAR_H * FONT_SCALE + 6;
        let bar_top = (y1 - bar_h as i32).max(0);
        // 靠右边界的框要左移标签，否则 "#2 FIRE 0.83" 会被画面边缘裁成 "#2"。
        let bar_w = text_w as i32 + 8;
        let bar_left = if x1 + bar_w > width as i32 {
            (width as i32 - bar_w).max(0)
        } else {
            x1.max(0)
        };
        fill_rect(
            buf,
            width,
            height,
            bar_left,
            bar_top,
            (bar_left + bar_w).min(width as i32),
            bar_top + bar_h as i32,
            color,
        );
        draw_text(
            buf,
            width,
            height,
            bar_left + 4,
            bar_top + 3,
            &text,
            [255, 255, 255],
        );
    }
}

// 绘制原语需要同时接收缓冲区、尺寸、两个角和颜色；拆成结构体只会在热路径上加一次解构。
#[allow(clippy::too_many_arguments)]
fn fill_rect(
    buf: &mut [u8],
    width: usize,
    height: usize,
    x1: i32,
    y1: i32,
    x2: i32,
    y2: i32,
    color: [u8; 3],
) {
    let xs = x1.clamp(0, width as i32);
    let xe = x2.clamp(0, width as i32);
    let ys = y1.clamp(0, height as i32);
    let ye = y2.clamp(0, height as i32);
    for y in ys..ye {
        let row = y as usize * width * 3;
        for x in xs..xe {
            let idx = row + x as usize * 3;
            buf[idx] = color[0];
            buf[idx + 1] = color[1];
            buf[idx + 2] = color[2];
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn draw_rect(
    buf: &mut [u8],
    width: usize,
    height: usize,
    x1: i32,
    y1: i32,
    x2: i32,
    y2: i32,
    color: [u8; 3],
    thickness: i32,
) {
    fill_rect(buf, width, height, x1, y1, x2, y1 + thickness, color);
    fill_rect(buf, width, height, x1, y2 - thickness, x2, y2, color);
    fill_rect(buf, width, height, x1, y1, x1 + thickness, y2, color);
    fill_rect(buf, width, height, x2 - thickness, y1, x2, y2, color);
}

fn draw_text(
    buf: &mut [u8],
    width: usize,
    height: usize,
    x: i32,
    y: i32,
    text: &str,
    color: [u8; 3],
) {
    let mut cursor = x;
    for ch in text.chars() {
        let Some(glyph) = glyph(ch) else {
            cursor += ((CHAR_W + CHAR_SPACING) * FONT_SCALE) as i32;
            continue;
        };
        for (col, bits) in glyph.iter().enumerate() {
            for row in 0..CHAR_H {
                if bits & (1 << row) == 0 {
                    continue;
                }
                let px = cursor + (col * FONT_SCALE) as i32;
                let py = y + (row * FONT_SCALE) as i32;
                fill_rect(
                    buf,
                    width,
                    height,
                    px,
                    py,
                    px + FONT_SCALE as i32,
                    py + FONT_SCALE as i32,
                    color,
                );
            }
        }
        cursor += ((CHAR_W + CHAR_SPACING) * FONT_SCALE) as i32;
    }
}

/// 5x7 点阵字库，每列一个字节，最低位为最上面一行。
fn glyph(c: char) -> Option<[u8; 5]> {
    let g = match c.to_ascii_uppercase() {
        ' ' => [0x00, 0x00, 0x00, 0x00, 0x00],
        '0' => [0x3E, 0x51, 0x49, 0x45, 0x3E],
        '1' => [0x00, 0x42, 0x7F, 0x40, 0x00],
        '2' => [0x42, 0x61, 0x51, 0x49, 0x46],
        '3' => [0x21, 0x41, 0x45, 0x4B, 0x31],
        '4' => [0x18, 0x14, 0x12, 0x7F, 0x10],
        '5' => [0x27, 0x45, 0x45, 0x45, 0x39],
        '6' => [0x3C, 0x4A, 0x49, 0x49, 0x30],
        '7' => [0x01, 0x71, 0x09, 0x05, 0x03],
        '8' => [0x36, 0x49, 0x49, 0x49, 0x36],
        '9' => [0x06, 0x49, 0x49, 0x29, 0x1E],
        'A' => [0x7E, 0x11, 0x11, 0x11, 0x7E],
        'B' => [0x7F, 0x49, 0x49, 0x49, 0x36],
        'C' => [0x3E, 0x41, 0x41, 0x41, 0x22],
        'D' => [0x7F, 0x41, 0x41, 0x22, 0x1C],
        'E' => [0x7F, 0x49, 0x49, 0x49, 0x41],
        'F' => [0x7F, 0x09, 0x09, 0x09, 0x01],
        'G' => [0x3E, 0x41, 0x49, 0x49, 0x7A],
        'H' => [0x7F, 0x08, 0x08, 0x08, 0x7F],
        'I' => [0x00, 0x41, 0x7F, 0x41, 0x00],
        'J' => [0x20, 0x40, 0x41, 0x3F, 0x01],
        'K' => [0x7F, 0x08, 0x14, 0x22, 0x41],
        'L' => [0x7F, 0x40, 0x40, 0x40, 0x40],
        'M' => [0x7F, 0x02, 0x0C, 0x02, 0x7F],
        'N' => [0x7F, 0x04, 0x08, 0x10, 0x7F],
        'O' => [0x3E, 0x41, 0x41, 0x41, 0x3E],
        'P' => [0x7F, 0x09, 0x09, 0x09, 0x06],
        'Q' => [0x3E, 0x41, 0x51, 0x21, 0x5E],
        'R' => [0x7F, 0x09, 0x19, 0x29, 0x46],
        'S' => [0x46, 0x49, 0x49, 0x49, 0x31],
        'T' => [0x01, 0x01, 0x7F, 0x01, 0x01],
        'U' => [0x3F, 0x40, 0x40, 0x40, 0x3F],
        'V' => [0x1F, 0x20, 0x40, 0x20, 0x1F],
        'W' => [0x3F, 0x40, 0x38, 0x40, 0x3F],
        'X' => [0x63, 0x14, 0x08, 0x14, 0x63],
        'Y' => [0x07, 0x08, 0x70, 0x08, 0x07],
        'Z' => [0x61, 0x51, 0x49, 0x45, 0x43],
        '.' => [0x00, 0x60, 0x60, 0x00, 0x00],
        ':' => [0x00, 0x36, 0x36, 0x00, 0x00],
        '#' => [0x14, 0x7F, 0x14, 0x7F, 0x14],
        '%' => [0x23, 0x13, 0x08, 0x64, 0x62],
        '-' => [0x08, 0x08, 0x08, 0x08, 0x08],
        '_' => [0x40, 0x40, 0x40, 0x40, 0x40],
        '/' => [0x20, 0x10, 0x08, 0x04, 0x02],
        '(' => [0x00, 0x1C, 0x22, 0x41, 0x00],
        ')' => [0x00, 0x41, 0x22, 0x1C, 0x00],
        '?' => [0x02, 0x01, 0x51, 0x09, 0x06],
        ',' => [0x00, 0x50, 0x30, 0x00, 0x00],
        _ => return None,
    };
    Some(g)
}
