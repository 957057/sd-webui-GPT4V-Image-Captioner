﻿import gradio as gr
import csv
import os
from tqdm import tqdm
import concurrent.futures
import shutil
import threading
import subprocess

from modules import script_callbacks

from lib import Translator
from lib.ImgProcessing import process_images_in_folder
from lib.Tag_Processor import count_tags_in_folder, generate_wordcloud, modify_tags_in_folder, generate_network_graph
from lib.GPT4V_run import run_openai_api, unique_elements
from lib.GPT4Vapi_utils import save_api_details, get_api_details


def modify_file_content(file_path, new_content, mode):
    if mode == "skip/跳过" and os.path.exists(file_path):
        print(f"Skip writing, as the file {file_path} already exists.")
        return

    if mode == "overwrite/覆盖" or not os.path.exists(file_path):
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(new_content)
        return

    with open(file_path, 'r+', encoding='utf-8') as file:
        existing_content = file.read()
        file.seek(0)
        if mode == "prepend/前置插入":
            combined_content = unique_elements(new_content, existing_content)
            file.write(combined_content)
            file.truncate()
        elif mode == "append/末尾追加":
            combined_content = unique_elements(existing_content, new_content)
            file.write(combined_content)
            file.truncate()
        else:
            raise ValueError("Invalid mode. Must be 'overwrite/覆盖', 'prepend/前置插入', or 'append/末尾追加'.")


should_stop = threading.Event()

def process_single_image(api_key, prompt, api_url, image_path, quality, timeout):
    save_api_details(api_key, api_url)
    caption = run_openai_api(image_path, prompt, api_key, api_url, quality, timeout)
    print(caption)
    return caption


def process_batch_images(api_key, prompt, api_url, image_dir, file_handling_mode, quality, timeout):
    should_stop.clear()
    save_api_details(api_key, api_url)
    results = []

    supported_image_formats = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tiff', '.tif')
    image_files = []
    for root, dirs, files in os.walk(image_dir):
        for file in files:
            if file.lower().endswith(supported_image_formats):
                image_files.append(os.path.join(root, file))

    def process_image(filename, file_handling_mode):
        image_path = os.path.join(image_dir, filename)
        base_filename = os.path.splitext(filename)[0]
        caption_filename = f"{base_filename}.txt"
        caption_path = os.path.join(image_dir, caption_filename)

        if file_handling_mode != "skip/跳过" or not os.path.exists(caption_path):
            caption = run_openai_api(image_path, prompt, api_key, api_url, quality, timeout)

            if caption.startswith("Error:") or caption.startswith("API error:"):
                return handle_error(image_path, caption_path, caption_filename, filename)
            else:
                modify_file_content(caption_path, caption, file_handling_mode)
                return filename, caption_path
        else:
            return filename, "Skipped because caption file already exists."

    def handle_error(image_path, caption_path, caption_filename, filename):
        parent_dir = os.path.dirname(image_dir)
        error_image_dir = os.path.join(parent_dir, "error_images")
        if not os.path.exists(error_image_dir):
            os.makedirs(error_image_dir)

        error_image_path = os.path.join(error_image_dir, filename)
        error_caption_path = os.path.join(error_image_dir, caption_filename)

        try:
            shutil.move(image_path, error_image_path)
            if os.path.exists(caption_path):
                shutil.move(caption_path, error_caption_path)
            return filename, "Error handled and image with its caption moved to error directory."
        except Exception as e:
            return filename, f"An unexpected error occurred while moving {filename} or {caption_filename}: {e}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for filename in image_files:
            future = executor.submit(process_image, filename, file_handling_mode)
            futures[future] = filename  # 将 future 和 filename 映射起来
        progress = tqdm(total=len(futures), desc="Processing images")

        try:
            for future in concurrent.futures.as_completed(futures):
                filename = futures[future]
                if should_stop.is_set():
                    for f in futures:
                        f.cancel()
                    print("Batch processing was stopped by the user.")
                    break
                try:
                    result = future.result()
                except Exception as e:
                    result = (filename, f"An exception occurred: {e}")
                    print(f"An exception occurred while processing {filename}: {e}")
                results.append(result)
                progress.update(1)
        finally:
            progress.close()
            executor.shutdown(wait=False)

    print(f"Processing complete. Total images processed: {len(results)}")
    return results


def process_batch_watermark_detection(api_key, prompt, api_url, image_dir, detect_file_handling_mode, quality, timeout,
                                      watermark_dir):
    should_stop.clear()
    save_api_details(api_key, api_url)
    results = []
    prompt = 'Is image have watermark'

    supported_image_formats = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tiff', '.tif')
    image_files = []
    for root, dirs, files in os.walk(image_dir):
        for file in files:
            if file.lower().endswith(supported_image_formats):
                image_files.append(os.path.join(root, file))

    def process_image(filename, detect_file_handling_mode, watermark_dir):
        image_path = os.path.join(image_dir, filename)
        caption = run_openai_api(image_path, prompt, api_key, api_url, quality, timeout)

        if caption.startswith("Error:") or caption.startswith("API error:"):
            return "error"

        # EOI是cog迷之误判？
        if 'Yes,' in caption and '\'EOI\'' not in caption:
            if detect_file_handling_mode == "copy/复制":
                shutil.copy(filename, watermark_dir)
            if detect_file_handling_mode == "move/移动":
                shutil.move(filename, watermark_dir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for filename in image_files:
            future = executor.submit(process_image, filename, detect_file_handling_mode, watermark_dir)
            futures[future] = filename  # 将 future 和 filename 映射起来
        progress = tqdm(total=len(futures), desc="Processing images")

        try:
            for future in concurrent.futures.as_completed(futures):
                filename = futures[future]  # 获取正在处理的文件名
                if should_stop.is_set():
                    for f in futures:
                        f.cancel()
                    print("Batch processing was stopped by the user.")
                    break
                try:
                    result = future.result()
                except Exception as e:
                    result = (filename, f"An exception occurred: {e}")
                    print(f"An exception occurred while processing {filename}: {e}")
                results.append(result)
                progress.update(1)
        finally:
            progress.close()
            executor.shutdown(wait=False)

    results = f"Total checked images: {len(results)}"
    return results


# 运行批处理
saved_api_key, saved_api_url = get_api_details()


def run_script(folder_path, keywords):
    keywords = keywords if keywords else "sorry,error"
    result = subprocess.run(
        [
            'python', './lib/Failed_Tagging_File_Screening.py',
            '--image_path', folder_path,
            '--keywords', keywords
        ],
        capture_output=True, text=True
    )
    return result.stdout if result.stdout else "No Output", result.stderr if result.stderr else "No Error"


def stop_batch_processing():
    should_stop.set()
    return "Attempting to stop batch processing. Please wait for the current image to finish."


# Define the path to your CSV file here
PROMPTS_CSV_PATH = "saved_prompts.csv"


# Function to save prompt to CSV
def save_prompt(prompt):
    print(f"Saving prompt: {prompt}")
    # Append prompt to CSV file, making sure not to duplicate prompts.
    with open(PROMPTS_CSV_PATH, 'a+', newline='', encoding='utf-8') as file:
        # Move to the start of the file to read existing prompts
        file.seek(0)
        reader = csv.reader(file)
        existing_prompts = [row[0] for row in reader]
        if prompt not in existing_prompts:
            writer = csv.writer(file)
            writer.writerow([prompt])
        # Move back to the end of the file for any further writes
        file.seek(0, os.SEEK_END)
    return gr.Dropdown(label="Saved Prompts", choices=get_prompts_from_csv(), type="value", interactive=True)


# Function to delete a prompt from CSV
def delete_prompt(prompt):
    lines = []
    with open(PROMPTS_CSV_PATH, 'r', newline='', encoding='utf-8') as readFile:
        reader = csv.reader(readFile)
        lines = [row for row in reader if row and row[0] != prompt]
    with open(PROMPTS_CSV_PATH, 'w', newline='', encoding='utf-8') as writeFile:
        writer = csv.writer(writeFile)
        writer.writerows(lines)
    return gr.Dropdown(label="Saved Prompts", choices=get_prompts_from_csv(), type="value", interactive=True)


# Function to get prompts from CSV for dropdown
def get_prompts_from_csv():
    if not os.path.exists(PROMPTS_CSV_PATH):
        return []  # If the CSV file does not exist, return an empty list
    with open(PROMPTS_CSV_PATH, 'r', newline='', encoding='utf-8') as file:
        reader = csv.reader(file)
        return [row[0] for row in reader if row]  # Don't include empty rows


saves_folder = "."


def process_tags(folder_path, top_n, tags_to_remove, tags_to_replace, new_tag, insert_position, translate, api_key,
                 api_url):
    # 解析删除标签
    tags_to_remove_list = tags_to_remove.split(',') if tags_to_remove else []
    tags_to_remove_list = [tag.strip() for tag in tags_to_remove_list]

    # 解析替换标签
    tags_to_replace_dict = {}
    if tags_to_replace:
        try:
            for pair in tags_to_replace.split(','):
                old_tag, new_replacement_tag = pair.split(':')
                tags_to_replace_dict[old_tag.strip()] = new_replacement_tag.strip()
        except ValueError:
            return "Error: Tags to replace must be in 'old_tag:new_tag' format separated by commas", None, None

    # 修改文件夹中的标签
    modify_tags_in_folder(folder_path, tags_to_remove_list, tags_to_replace_dict, new_tag, insert_position)

    # 词云
    top = int(top_n)
    tag_counts = count_tags_in_folder(folder_path, top)

    # 截断过长的标签名称，只保留前max_length个字符，并在末尾添加省略号
    def truncate_tag(tag, max_length=30):
        return (tag[:max_length] + '...') if len(tag) > max_length else tag

    wordcloud_path = generate_wordcloud(tag_counts)
    network_graph_path = generate_network_graph(folder_path, top)

    # 翻译Tag功能
    if translate.startswith('GPT-3.5 translation / GPT3.5翻译'):
        translator = Translator.GPTTranslator(api_key, api_url)
    elif translate.startswith('Free translation / 免费翻译'):
        translator = Translator.ChineseTranslator()
    else:
        translator = None
    if translator:
        tags_to_translate = [tag for tag, _ in tag_counts]
        translations = Translator.translate_tags(translator, tags_to_translate)
        # 确保 translations 列表长度与 tag_counts 一致
        translations.extend(["" for _ in range(len(tag_counts) - len(translations))])
        tag_counts_with_translation = [(truncate_tag(tag_counts[i][0]), tag_counts[i][1], translations[i]) for i in
                                       range(len(tag_counts))]
    else:
        tag_counts_with_translation = [(truncate_tag(tag), count, "") for tag, count in tag_counts]

    return tag_counts_with_translation, wordcloud_path, network_graph_path, "Tags processed successfully."


os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"


def on_ui_tabs():
    
    with gr.Blocks(analytics_enabled=False) as GPT4V_captioner_tabs:
        
        gr.Markdown("### Image Captioning with GPT-4-Vision API / 使用 GPT-4-Vision API 进行图像打标")

        with gr.Row():
            api_key_input = gr.Textbox(label="API Key", placeholder="Enter your GPT-4-Vision API Key here", type="password",
                                       value=saved_api_key)
            api_url_input = gr.Textbox(label="API URL", value=saved_api_url or "https://api.openai.com/v1/chat/completions",
                                       placeholder="Enter the GPT-4-Vision API URL here")
            quality_choices = [
                ("Auto / 自动", "auto"),
                ("High Detail - More Expensive / 高细节-更贵", "high"),
                ("Low Detail - Cheaper / 低细节-更便宜", "low")
            ]
            quality = gr.Dropdown(choices=quality_choices, label="Image Quality / 图片质量", value="auto")
            timeout_input = gr.Number(label="Timeout (seconds) / 超时时间（秒）", value=10, step=1)

        prompt_input = gr.Textbox(label="Prompt / 打标需求",
                                  value="As an AI image tagging expert, please provide precise tags for these images to enhance CLIP model's understanding of the content. Employ succinct keywords or phrases, steering clear of elaborate sentences and extraneous conjunctions. Prioritize the tags by relevance. Your tags should capture key elements such as the main subject, setting, artistic style, composition, image quality, color tone, filter, and camera specifications, and any other tags crucial for the image. When tagging photos of people, include specific details like gender, nationality, attire, actions, pose, expressions, accessories, makeup, composition type, age, etc. For other image categories, apply appropriate and common descriptive tags as well. Recognize and tag any celebrities, well-known landmark or IPs if clearly featured in the image. Your tags should be accurate, non-duplicative, and within a 20-75 word count range. These tags will use for image re-creation, so the closer the resemblance to the original image, the better the tag quality. Tags should be comma-separated. Exceptional tagging will be rewarded with $10 per image.",
                                  placeholder="Enter a descriptive prompt",
                                  lines=5)

        with gr.Accordion("Prompt Saving / 提示词存档", open=False):
            saved_prompts = get_prompts_from_csv()
            saved_prompts_dropdown = gr.Dropdown(label="Saved Prompts / 提示词存档", choices=saved_prompts, type="value",
                                                 interactive=True)


            def update_textbox(prompt):
                return prompt


            with gr.Row():
                save_prompt_button = gr.Button("Save Prompt / 保存提示词")
                delete_prompt_button = gr.Button("Delete Prompt / 删除提示词")
                load_prompt_button = gr.Button("Load Prompt / 读取到输入框")
            save_prompt_button.click(save_prompt, inputs=prompt_input, outputs=saved_prompts_dropdown)
            load_prompt_button.click(update_textbox, inputs=saved_prompts_dropdown, outputs=prompt_input)
            delete_prompt_button.click(delete_prompt, inputs=saved_prompts_dropdown, outputs=saved_prompts_dropdown)

        with gr.Tab("Single Image / 单图处理"):
            with gr.Row():
                image_input = gr.Image(type='filepath', label="Upload Image / 上传图片")
                single_image_output = gr.Textbox(label="Caption Output / 标签输出")
            with gr.Row():
                single_image_submit = gr.Button("Caption Single Image / 图片打标", variant='primary')

        with gr.Tab("Batch Image / 多图批处理"):
            with gr.Row():
                batch_dir_input = gr.Textbox(label="Batch Directory / 批量目录",
                                             placeholder="Enter the directory path containing images for batch processing")
            with gr.Row():
                batch_process_submit = gr.Button("Batch Process Images / 批量处理图像", variant='primary')
            with gr.Row():
                batch_output = gr.Textbox(label="Batch Processing Output / 批量输出")
                file_handling_mode = gr.Radio(
                    choices=["overwrite/覆盖", "prepend/前置插入", "append/末尾追加", "skip/跳过"],
                    value="overwrite/覆盖",
                    label="If a caption file exists: / 如果已经存在打标文件: "
                )
            with gr.Row():
                stop_button = gr.Button("Stop Batch Processing / 停止批量处理")
                stop_button.click(stop_batch_processing, inputs=[], outputs=batch_output)

        with gr.Tab("Failed File Screening / 打标失败文件筛查"):
            folder_input = gr.Textbox(label="Folder Input / 文件夹输入", placeholder="Enter the directory path")
            keywords_input = gr.Textbox(placeholder="Enter keywords, e.g., sorry,error / 请输入检索关键词，例如：sorry,error",
                                        label="Keywords (optional) / 检索关键词（可选）")
            run_button = gr.Button("Run Script / 运行脚本", variant='primary')
            output_area = gr.Textbox(label="Script Output / 脚本输出")

            run_button.click(fn=run_script, inputs=[folder_input, keywords_input], outputs=output_area)

        with gr.Tab("Tag Manage / 标签处理"):

            with gr.Row():
                folder_path_input = gr.Textbox(label="Folder Path / 文件夹路径",
                                               placeholder="Enter folder path / 在此输入文件夹路径")
                top_n_input = gr.Number(label="Top N Tags / Top N 标签", value=100)
                translate_tags_input = gr.Radio(label="Translate Tags to Chinese / 翻译标签",
                                                choices=["GPT-3.5 translation / GPT3.5翻译",
                                                         "Free translation / 免费翻译",
                                                         "No translation / 不翻译"],
                                                value="No translation / 不翻译")
                process_tags_button = gr.Button("Process Tags / 处理标签", variant='primary')
                output_message = gr.Textbox(label="Output Message / 输出信息", interactive=False)

            with gr.Row():
                tags_to_remove_input = gr.Textbox(label="Tags to Remove / 删除标签",
                                                  placeholder="Enter tags to remove, separated by commas / 输入要删除的标签，用逗号分隔",
                                                  lines=3)
                tags_to_replace_input = gr.Textbox(label="Tags to Replace / 替换标签",
                                                   placeholder="Enter tags to replace in 'old_tag:new_tag' format, separated by commas / 输入要替换的标签，格式为 '旧标签:新标签'，用逗号分隔",
                                                   lines=3)
                new_tag_input = gr.Textbox(label="Add New Tag / 添加新标签",
                                           placeholder="Enter a new tag to add / 输入一个新标签以添加", lines=3)
                insert_position_input = gr.Radio(label="New Tag Insert Position / 新标签插入位置",
                                                 choices=["Start / 开始", "End / 结束", "Random / 随机"],
                                                 value="Start / 开始")

            with gr.Row():
                wordcloud_output = gr.Image(label="Word Cloud / 词云")
                tag_counts_output = gr.Dataframe(label="Top Tags / 高频标签",
                                                 headers=["Tag Name", "Frequency", "Chinese Translation"],
                                                 interactive=True)  # 修改 Dataframe 组件以显示三列

            with gr.Row():
                network_graph_output = gr.Image(label="Network Graph / 网络图")

            process_tags_button.click(
                process_tags,
                inputs=[
                    folder_path_input, top_n_input, tags_to_remove_input,
                    tags_to_replace_input, new_tag_input, insert_position_input,
                    translate_tags_input,  # 新增翻译复选框
                    api_key_input, api_url_input
                ],
                outputs=[tag_counts_output, wordcloud_output, network_graph_output, output_message]
            )

        with gr.Tab("Image Zip / 图像预压缩"):
            with gr.Row():
                folder_path_input = gr.Textbox(
                    label="Image Folder Path / 图像文件夹路径",
                    placeholder="Enter the folder path containing images / 输入包含图像的文件夹路径"
                )
                process_images_button = gr.Button("Process Images / 压缩图像")

            with gr.Row():
                # Add a Markdown component to display the warning message
                gr.Markdown("""
            ⚠ **Warning / 警告**: This preprocessing process will resize and compress all image files into jpg format with a total pixel count ≤ 1024×1024 while maintaining the original aspect ratio, ensuring that both dimensions are multiples of 32. **Please make sure to backup your original files before processing!** This procedure can reduce the size of the training set, help to speed up the labeling process, and decrease the time taken to cache latents to disk during training.

            本预处理过程将会在保持原图长宽比情况下，把所有图像文件裁剪压缩为总像素≤1024×1024的jpg文件，并且长宽像素均为32的倍数。**请务必在处理前备份源文件！**该过程可以缩小训练集体积，有助于加快打标速度，并缩短训练过程中的Cache latents to disk时间。
                """)

            with gr.Row():
                image_processing_output = gr.Textbox(
                    label="Image Processing Output / 图像处理输出",
                    lines=3
                )

            process_images_button.click(
                fn=process_images_in_folder,
                inputs=[folder_path_input],
                outputs=[image_processing_output]
            )

        with gr.Tab("Watermark Detection / 批量水印检测"):
            gr.Markdown("""
                    本功能完全是基于CogVLM开发（GPT4未经测试），极力推荐使用CogVLM-vqa以达到最佳效果。\n
                    This function is fully developed based on CogVLM (GPT4 not tested), and it is strongly recommended to use CogVLM-vqa for optimal results.
                    """)
            with gr.Row():
                detect_batch_dir_input = gr.Textbox(label="Image Directory / 图片目录",
                                                    placeholder="Enter the directory path containing images for batch processing")
            with gr.Row():
                watermark_dir = gr.Textbox(label="Watermark Detected Image Directory / 检测到水印的图片目录",
                                           placeholder="Enter the directory path to move/copy detected images")
                detect_file_handling_mode = gr.Radio(choices=["move/移动", "copy/复制"], value="move/移动",
                                                     label="If watermark is detected / 如果图片检测到水印 ")
            with gr.Row():
                batch_detect_submit = gr.Button("Batch Detect Images / 批量检测图像", variant='primary')
            with gr.Row():
                detect_batch_output = gr.Textbox(label="Output / 结果")
            with gr.Row():
                detect_stop_button = gr.Button("Stop Batch Processing / 停止批量处理")
                detect_stop_button.click(stop_batch_processing, inputs=[], outputs=detect_batch_output)

        def batch_process(api_key, api_url, prompt, batch_dir, file_handling_mode, quality, timeout):
            process_batch_images(api_key, prompt, api_url, batch_dir, file_handling_mode, quality, timeout)
            return "Batch processing complete. Captions saved or updated as '.txt' files next to images."


        def batch_detect(api_key, api_url, prompt, batch_dir, detect_file_handling_mode, quality, timeout, watermark_dir):
            results = process_batch_watermark_detection(api_key, prompt, api_url, batch_dir, detect_file_handling_mode,
                                                        quality, timeout,
                                                        watermark_dir)
            return results


        def caption_image(api_key, api_url, prompt, image, quality, timeout):
            if image:
                return process_single_image(api_key, prompt, api_url, image, quality, timeout)


        single_image_submit.click(caption_image,
                                  inputs=[api_key_input, api_url_input, prompt_input, image_input, quality, timeout_input],
                                  outputs=single_image_output)
        batch_process_submit.click(
            batch_process,
            inputs=[api_key_input, api_url_input, prompt_input, batch_dir_input, file_handling_mode, quality,
                    timeout_input],
            outputs=batch_output
        )
        batch_detect_submit.click(
            batch_detect,
            inputs=[api_key_input, api_url_input, prompt_input, detect_batch_dir_input, detect_file_handling_mode, quality,
                    timeout_input, watermark_dir],
            outputs=detect_batch_output
        )
        gr.Markdown(
            "### Developers: [Jiaye](https://civitai.com/user/jiayev1),&nbsp;&nbsp;[LEOSAM 是只兔狲](https://civitai.com/user/LEOSAM),&nbsp;&nbsp;[SleeeepyZhou](https://civitai.com/user/SleeeepyZhou),&nbsp;&nbsp;[Fok](https://civitai.com/user/fok3827)&nbsp;&nbsp;|&nbsp;&nbsp;Welcome everyone to add more new features to this project.")

    return [(GPT4V_captioner_tabs, "GPT4V打标器", "GPT4V Captioner")]


def on_ui_settings():
    section = ('extension', "GPT4V-Image-Captioner") 

script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_ui_tabs(on_ui_tabs)