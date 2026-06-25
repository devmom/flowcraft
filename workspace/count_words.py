#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件字数统计脚本
用法: python count_words.py <文件路径>
功能: 统计文本文件的总字数、行数、字符数
"""

import sys
import os
import re


def count_words_in_file(file_path):
    """
    统计文件中的字数、行数和字符数
    
    Args:
        file_path: 要统计的文件路径
    
    Returns:
        dict: 包含统计结果的字典
    """
    if not os.path.exists(file_path):
        print(f"错误: 文件 '{file_path}' 不存在")
        sys.exit(1)
    
    if not os.path.isfile(file_path):
        print(f"错误: '{file_path}' 不是一个文件")
        sys.exit(1)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        # 尝试其他编码
        try:
            with open(file_path, 'r', encoding='gbk') as f:
                content = f.read()
        except Exception as e:
            print(f"错误: 无法读取文件 '{file_path}': {e}")
            sys.exit(1)
    except Exception as e:
        print(f"错误: 无法读取文件 '{file_path}': {e}")
        sys.exit(1)
    
    # 统计行数
    lines = content.split('\n')
    line_count = len(lines)
    # 如果文件以换行符结尾，最后一行是空行，不计入
    if content.endswith('\n'):
        line_count -= 1
    
    # 统计字符数（包含空格和标点）
    char_count = len(content)
    
    # 统计字符数（不含空格）
    char_count_no_spaces = len(content.replace(' ', '').replace('\n', '').replace('\r', '').replace('\t', ''))
    
    # 统计字数（中英文混合）
    # 英文单词：按空白字符分割
    # 中文字符：每个汉字算一个字
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', content)
    chinese_count = len(chinese_chars)
    
    # 英文单词数
    # 移除中文字符后，按空白分割统计英文单词
    text_without_chinese = re.sub(r'[\u4e00-\u9fff]', ' ', content)
    english_words = text_without_chinese.split()
    english_word_count = len(english_words)
    
    # 总字数 = 中文字数 + 英文单词数
    total_words = chinese_count + english_word_count
    
    return {
        'file_path': file_path,
        'file_name': os.path.basename(file_path),
        'file_size': os.path.getsize(file_path),
        'total_words': total_words,
        'chinese_chars': chinese_count,
        'english_words': english_word_count,
        'lines': line_count,
        'characters': char_count,
        'characters_no_spaces': char_count_no_spaces,
    }


def print_results(stats):
    """打印统计结果"""
    print("=" * 50)
    print(f"文件字数统计报告")
    print("=" * 50)
    print(f"文件路径:     {stats['file_path']}")
    print(f"文件名:       {stats['file_name']}")
    print(f"文件大小:     {stats['file_size']:,} 字节")
    print("-" * 50)
    print(f"总字数:       {stats['total_words']:,}")
    print(f"  - 中文字符:  {stats['chinese_chars']:,}")
    print(f"  - 英文单词:  {stats['english_words']:,}")
    print(f"行数:         {stats['lines']:,}")
    print(f"字符数(含空格): {stats['characters']:,}")
    print(f"字符数(无空格): {stats['characters_no_spaces']:,}")
    print("=" * 50)


def main():
    if len(sys.argv) < 2:
        print("用法: python count_words.py <文件路径>")
        print("示例: python count_words.py example.txt")
        sys.exit(1)
    
    file_path = sys.argv[1]
    stats = count_words_in_file(file_path)
    print_results(stats)


if __name__ == '__main__':
    main()
